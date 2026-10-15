"""Prometheus business metrics, updated by the consumer (docs/week3-plan.md
W3-D10) — live Grafana panels without a Mongo datasource. The Mongo API
(app/queries.py) stays the source for exact numbers over any window.

Each is incremented only when an event changes the read model for the
first time (app/facts.py), so a replayed event doesn't count twice.
Counters restart at zero with the process, which Prometheus' rate() and
increase() handle.

Label values are bounded: reasons are normalized to known codes
(sfo_events.reasons), so free-text cancellation reasons can't explode the
series count. `restaurant_id` grows with the number of restaurants —
fine at this scale; aggregate it away at scale."""

from prometheus_client import Counter, Histogram

ORDERS_PLACED = Counter("orders_placed_total", "Orders placed", ["restaurant_id"])
ORDER_OUTCOMES = Counter(
    "order_outcomes_total",
    "Orders reaching a terminal status",
    ["outcome", "reason"],  # outcome: delivered | rejected | cancelled
)
RESTAURANT_RESPONSES = Counter(
    "restaurant_responses_total",
    "Restaurant decisions on new orders",
    ["restaurant_id", "response"],  # accepted | rejected | timeout
)
DELIVERY_DURATION = Histogram(
    "delivery_duration_seconds",
    "Time from order placed to delivered",
    # Short buckets too: the scripted test orders complete in seconds.
    buckets=(10, 30, 60, 300, 600, 900, 1200, 1800, 2700, 3600, 5400),
)
RIDER_SHIFT_CHANGES = Counter(
    "rider_shift_changes_total", "Riders going on or off shift", ["on_shift"]
)
DEAD_LETTERED = Counter(
    "dead_lettered_events_total", "Messages consumers moved to a DLQ", ["topic", "consumer_group"]
)
