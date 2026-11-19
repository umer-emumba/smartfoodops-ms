"""Generates the Grafana dashboards in infra/grafana/dashboards/ (provisioned
at Grafana's start-up). Edit here, then run:

    uv run python infra/grafana/build_dashboards.py

Six dashboards (docs/week3-plan.md step 5, docs/week4-plan.md step 5,
docs/week5-plan.md step 7):
- Service overview: requests, errors and latency per service (RED).
- Orders & deliveries: the business flow — orders, outcomes, restaurant
  responses, delivery time, sagas, riders, live tracking.
- Event pipeline: outbox → Kafka → consumers → Celery → notifications, plus
  Temporal activities and RabbitMQ.
- Logs: every container's logs from Loki, filterable, with trace links.
- AI / Retrieval: search latency and results, embedding, the indexer's lag
  and health.
- AI Assistant (docs/week5-plan.md step 7): turns, answer time against the
  targets, LLM errors by type, checks catching the model, tokens and cost.
"""

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}


class Layout:
    """Places panels left to right, 24 columns wide, wrapping rows."""

    def __init__(self) -> None:
        self.x = self.y = self.row_height = 0
        self.next_id = 1

    def place(self, width: int, height: int) -> dict[str, int]:
        if self.x + width > 24:
            self.x, self.y = 0, self.y + self.row_height
            self.row_height = 0
        pos = {"x": self.x, "y": self.y, "w": width, "h": height}
        self.x += width
        self.row_height = max(self.row_height, height)
        return pos

    def new_row(self) -> None:
        if self.x:
            self.x, self.y, self.row_height = 0, self.y + self.row_height, 0

    def id(self) -> int:
        self.next_id += 1
        return self.next_id


def _targets(queries: list[tuple[str, str]], datasource: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"datasource": datasource, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
        for i, (expr, legend) in enumerate(queries)
    ]


def timeseries(
    layout: Layout,
    title: str,
    queries: list[tuple[str, str]],
    *,
    unit: str = "short",
    width: int = 12,
    height: int = 8,
    stack: bool = False,
    description: str = "",
) -> dict[str, Any]:
    return {
        "id": layout.id(),
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": PROM,
        "gridPos": layout.place(width, height),
        "targets": _targets(queries, PROM),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "fillOpacity": 15 if stack else 5,
                    "stacking": {"mode": "normal" if stack else "none"},
                    "showPoints": "never",
                },
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }


def stat(
    layout: Layout,
    title: str,
    expr: str,
    *,
    unit: str = "short",
    width: int = 4,
    thresholds: list[tuple[float | None, str]] | None = None,
    description: str = "",
) -> dict[str, Any]:
    steps = [{"value": value, "color": color} for value, color in thresholds or [(None, "blue")]]
    return {
        "id": layout.id(),
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROM,
        "gridPos": layout.place(width, 4),
        "targets": _targets([(expr, "")], PROM),
        "fieldConfig": {
            "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
            "overrides": [],
        },
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"},
    }


def alerts_panel(layout: Layout) -> dict[str, Any]:
    return {
        "id": layout.id(),
        "type": "table",
        "title": "Firing alerts (infra/prometheus/rules.yml)",
        "datasource": PROM,
        "gridPos": layout.place(24, 6),
        "targets": [
            {
                "datasource": PROM,
                "expr": 'ALERTS{alertstate="firing"}',
                "format": "table",
                "instant": True,
                "refId": "A",
            }
        ],
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True, "Value": True, "__name__": True},
                },
            }
        ],
    }


def dashboard(uid: str, title: str, panels: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "tags": ["smartfoodops"],
        "timezone": "browser",
        "schemaVersion": 39,
        "time": {"from": "now-1h", "to": "now"},
        "refresh": "30s",
        "panels": panels,
        **extra,
    }


def service_overview() -> dict[str, Any]:
    lay = Layout()
    rate_5xx = 'sum by (service) (rate(http_requests_total{status=~"5.."}[5m]))'
    rate_all = "sum by (service) (rate(http_requests_total[5m]))"
    panels = [
        alerts_panel(lay),
        timeseries(lay, "Requests / s by service", [(rate_all, "{{service}}")], unit="reqps"),
        timeseries(
            lay,
            "5xx error ratio by service",
            # `or ... * 0`: a 0 line for services with no 5xx at all.
            [(f"({rate_5xx} or {rate_all} * 0) / {rate_all}", "{{service}}")],
            unit="percentunit",
            description="Alert HighErrorRate fires above 1% for 5 minutes.",
        ),
        timeseries(
            lay,
            "p95 latency by service",
            [
                (
                    "histogram_quantile(0.95, sum by (service, le) "
                    "(rate(http_request_duration_seconds_bucket[5m])))",
                    "{{service}}",
                )
            ],
            unit="s",
        ),
        timeseries(
            lay,
            "4xx by service",
            [('sum by (service) (rate(http_requests_total{status=~"4.."}[5m]))', "{{service}}")],
            unit="reqps",
        ),
        timeseries(
            lay,
            "Slowest routes (p95)",
            [
                (
                    "topk(8, histogram_quantile(0.95, sum by (service, route, le) "
                    "(rate(http_request_duration_seconds_bucket[5m]))))",
                    "{{service}} {{route}}",
                )
            ],
            unit="s",
            width=24,
        ),
        timeseries(
            lay,
            "Scrape targets up",
            [("sum by (job) (up)", "{{job}} up"), ("sum by (job) (1 - up)", "{{job}} down")],
            width=24,
            height=6,
        ),
    ]
    return dashboard("sfo-services", "SmartFoodOps — Service overview (RED)", panels)


def business() -> dict[str, Any]:
    lay = Layout()
    accepted = 'sum(increase(restaurant_responses_total{response="accepted"}[$__range]))'
    decided = "sum(increase(restaurant_responses_total[$__range]))"
    placed = "sum(increase(orders_placed_total[$__range]))"
    panels = [
        stat(lay, "Orders placed", f"round({placed})"),
        stat(
            lay,
            "Delivered",
            'round(sum(increase(order_outcomes_total{outcome="delivered"}[$__range])))',
        ),
        stat(
            lay,
            "Cancellation rate",
            f'sum(increase(order_outcomes_total{{outcome="cancelled"}}[$__range])) / {placed}',
            unit="percentunit",
        ),
        stat(
            lay,
            "Restaurant acceptance",
            f"{accepted} / {decided}",
            unit="percentunit",
            thresholds=[(None, "red"), (0.7, "orange"), (0.9, "green")],
        ),
        stat(
            lay,
            "Median delivery time",
            "histogram_quantile(0.5, sum by (le) "
            "(increase(delivery_duration_seconds_bucket[$__range])))",
            unit="s",
        ),
        stat(
            lay,
            "Saga system errors",
            'round(sum(increase(saga_outcomes_total{reason="system_error"}[$__range]))) '
            "or vector(0)",
            thresholds=[(None, "green"), (1, "red")],
        ),
        timeseries(
            lay,
            "Orders placed / min by restaurant",
            [("sum by (restaurant_id) (rate(orders_placed_total[5m])) * 60", "{{restaurant_id}}")],
            stack=True,
        ),
        timeseries(
            lay,
            "Order outcomes / min",
            [
                (
                    "sum by (outcome, reason) (rate(order_outcomes_total[5m])) * 60",
                    "{{outcome}} {{reason}}",
                )
            ],
            stack=True,
        ),
        timeseries(
            lay,
            "Restaurant responses / min",
            [
                (
                    "sum by (response) (rate(restaurant_responses_total[5m])) * 60",
                    "{{response}}",
                )
            ],
            stack=True,
        ),
        timeseries(
            lay,
            "Delivery time (placed → delivered)",
            [
                (
                    "histogram_quantile(0.5, sum by (le) "
                    "(rate(delivery_duration_seconds_bucket[15m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) "
                    "(rate(delivery_duration_seconds_bucket[15m])))",
                    "p95",
                ),
            ],
            unit="s",
        ),
        timeseries(
            lay,
            "Saga outcomes / min",
            [
                (
                    "sum by (status, reason) (rate(saga_outcomes_total[5m])) * 60",
                    "{{status}} {{reason}}",
                )
            ],
            stack=True,
            description=(
                "From order-service as each saga ends; "
                "analytics-service has exact counts over any window."
            ),
        ),
        timeseries(
            lay,
            "Rider pool (approved riders)",
            [("sum by (status) (riders)", "{{status}}")],
            stack=True,
        ),
        timeseries(
            lay,
            "Live tracking streams",
            [("sum by (feed) (tracking_active_streams)", "{{feed}}")],
        ),
        timeseries(
            lay,
            "Rider shift changes / min",
            [
                (
                    "sum by (on_shift) (rate(rider_shift_changes_total[5m])) * 60",
                    "on shift={{on_shift}}",
                )
            ],
        ),
    ]
    return dashboard("sfo-business", "SmartFoodOps — Orders & deliveries", panels)


def pipeline() -> dict[str, Any]:
    lay = Layout()
    panels = [
        stat(
            lay,
            "Oldest unpublished event",
            "max(outbox_oldest_age_seconds)",
            unit="s",
            thresholds=[(None, "green"), (10, "orange"), (30, "red")],
            description="Alert OutboxLagging fires above 30s for 1 minute.",
        ),
        stat(lay, "Outbox backlog", "sum(outbox_backlog)"),
        stat(
            lay,
            "Consumer lag",
            "sum(kafka_consumer_lag)",
            thresholds=[(None, "green"), (100, "orange")],
        ),
        stat(
            lay,
            "Dead-lettered (range)",
            'round(sum(increase(kafka_events_consumed_total{outcome="dead_lettered"}[$__range]))) '
            "or vector(0)",
            thresholds=[(None, "green"), (1, "red")],
        ),
        stat(
            lay,
            "Notifications failed (range)",
            'round(sum(increase(notification_sends_total{result="failed"}[$__range]))) '
            "or vector(0)",
            thresholds=[(None, "green"), (1, "red")],
        ),
        stat(
            lay,
            "Celery task failures (range)",
            'round(sum(increase(celery_tasks_total{outcome="failed"}[$__range]))) or vector(0)',
            thresholds=[(None, "green"), (1, "red")],
        ),
        timeseries(
            lay,
            "Outbox: oldest unpublished event by service",
            [("max by (service) (outbox_oldest_age_seconds)", "{{service}}")],
            unit="s",
        ),
        timeseries(
            lay,
            "Outbox: published / failed per second",
            [
                ("sum by (service) (rate(outbox_published_total[5m]))", "{{service}} published"),
                (
                    "sum by (service) (rate(outbox_publish_failures_total[5m]))",
                    "{{service}} failed",
                ),
            ],
            unit="ops",
        ),
        timeseries(
            lay,
            "Consumed / s by group and outcome",
            [
                (
                    "sum by (group, outcome) (rate(kafka_events_consumed_total[5m]))",
                    "{{group}} {{outcome}}",
                ),
                (
                    "sum by (group) (rate(kafka_events_duplicates_total[5m]))",
                    "{{group}} duplicates (already applied)",
                ),
            ],
            unit="ops",
            description=(
                "Duplicates are redeliveries the consumer had already applied — "
                "a no-op, and also counted as processed. A burst means a replay."
            ),
        ),
        timeseries(
            lay,
            "Consumer lag by group and topic",
            [("sum by (group, topic) (kafka_consumer_lag)", "{{group}} {{topic}}")],
        ),
        timeseries(
            lay,
            "Consumer retries / min",
            [
                (
                    "sum by (group, kind) (rate(kafka_event_retries_total[5m])) * 60",
                    "{{group}} {{kind}}",
                )
            ],
            description=(
                "handler: the handler failed (3 retries, then DLQ). "
                "outage: a dependency was down (waits it out)."
            ),
        ),
        timeseries(
            lay,
            "Celery tasks / s by outcome",
            [
                (
                    "sum by (service, task, outcome) (rate(celery_tasks_total[5m]))",
                    "{{task}} {{outcome}}",
                )
            ],
            unit="ops",
        ),
        timeseries(
            lay,
            "Notification sends / min",
            [
                (
                    "sum by (channel, result) (rate(notification_sends_total[5m])) * 60",
                    "{{channel}} {{result}}",
                )
            ],
            stack=True,
        ),
        timeseries(
            lay,
            "RabbitMQ: messages waiting (all queues)",
            [
                ("sum(rabbitmq_queue_messages_ready)", "ready"),
                (
                    "sum(rabbitmq_queue_messages_unacked)",
                    "unacked (in progress or retry countdown)",
                ),
            ],
        ),
        timeseries(
            lay,
            "Temporal: activity failures / min",
            [
                (
                    "sum by (service, activity_type) "
                    "(rate(temporal_activity_execution_failed[5m])) * 60",
                    "{{service}} {{activity_type}}",
                )
            ],
        ),
        timeseries(
            lay,
            "Temporal: activity latency p95",
            [
                (
                    "histogram_quantile(0.95, sum by (activity_type, le) "
                    "(rate(temporal_activity_execution_latency_bucket[5m])))",
                    "{{activity_type}}",
                )
            ],
            unit="s",
        ),
    ]
    return dashboard("sfo-pipeline", "SmartFoodOps — Event pipeline", panels)


def logs() -> dict[str, Any]:
    lay = Layout()
    selector = '{service=~"$service", level=~"$level"}'
    panels = [
        {
            "id": lay.id(),
            "type": "timeseries",
            "title": "Log lines by level",
            "datasource": LOKI,
            "gridPos": lay.place(24, 6),
            "targets": [
                {
                    "datasource": LOKI,
                    "expr": (
                        f"sum by (level) (count_over_time({selector} |= `$search` [$__interval]))"
                    ),
                    "legendFormat": "{{level}}",
                    "refId": "A",
                }
            ],
            "fieldConfig": {
                "defaults": {"custom": {"drawStyle": "bars", "stacking": {"mode": "normal"}}},
                "overrides": [],
            },
        },
        {
            "id": lay.id(),
            "type": "logs",
            "title": "Logs (expand a line: its trace_id links to the trace)",
            "datasource": LOKI,
            "gridPos": lay.place(24, 20),
            "targets": [{"datasource": LOKI, "expr": f"{selector} |= `$search`", "refId": "A"}],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"},
        },
    ]
    variables = [
        {
            "name": "service",
            "label": "Service",
            "type": "query",
            "datasource": LOKI,
            "query": {"label": "service", "stream": "", "type": 1},
            "includeAll": True,
            "allValue": ".+",
            "multi": True,
            "current": {"text": "All", "value": "$__all"},
            "refresh": 2,
        },
        {
            "name": "level",
            "label": "Level",
            "type": "custom",
            "query": "ERROR,WARNING,INFO,DEBUG,CRITICAL",
            "includeAll": True,
            "allValue": ".*",
            "multi": True,
            "current": {"text": "All", "value": "$__all"},
        },
        {
            "name": "search",
            "label": "Contains (e.g. a request_id or trace_id)",
            "type": "textbox",
            "query": "",
            "current": {"text": "", "value": ""},
        },
    ]
    return dashboard("sfo-logs", "SmartFoodOps — Logs", panels, templating={"list": variables})


def ai_retrieval() -> dict[str, Any]:
    lay = Layout()
    search_p95 = (
        "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
        '{service="ai-service", route=~"/v1/ai/search.*"}[5m])))'
    )
    indexer = 'group="ai-indexer"'
    panels = [
        stat(
            lay,
            "Search p95 (whole request)",
            search_p95,
            unit="s",
            thresholds=[(None, "green"), (0.3, "orange"), (1, "red")],
        ),
        stat(
            lay,
            "Index lag p95",
            "histogram_quantile(0.95, sum by (le) (rate(ai_index_lag_seconds_bucket[5m])))",
            unit="s",
            thresholds=[(None, "green"), (5, "orange"), (30, "red")],
            description="Event written → document searchable. Alert IndexLagging above 30s.",
        ),
        stat(
            lay,
            "Indexer consumer lag",
            f"sum(kafka_consumer_lag{{{indexer}}}) or vector(0)",
            thresholds=[(None, "green"), (100, "orange")],
        ),
        stat(
            lay,
            "Indexer dead-lettered (range)",
            f'round(sum(increase(kafka_events_consumed_total{{{indexer}, outcome="dead_lettered"}}'
            "[$__range]))) or vector(0)",
            thresholds=[(None, "green"), (1, "red")],
        ),
        stat(
            lay,
            "Indexer outage retries (range)",
            f'round(sum(increase(kafka_event_retries_total{{{indexer}, kind="outage"}}'
            "[$__range]))) or vector(0)",
            thresholds=[(None, "green"), (1, "orange")],
            description="Qdrant (or Kafka) unreachable: the event is retried, never dropped.",
        ),
        stat(
            lay,
            "Empty searches (range)",
            'round(sum(increase(ai_searches_total{result="empty"}[$__range]))) or vector(0)',
        ),
        timeseries(
            lay,
            "Searches / s by kind and result",
            [("sum by (kind, result) (rate(ai_searches_total[5m]))", "{{kind}} {{result}}")],
            unit="reqps",
        ),
        timeseries(
            lay,
            "Qdrant query p50 / p95 by kind",
            [
                (
                    "histogram_quantile(0.5, sum by (kind, le) "
                    "(rate(ai_qdrant_query_seconds_bucket[5m])))",
                    "{{kind}} p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (kind, le) "
                    "(rate(ai_qdrant_query_seconds_bucket[5m])))",
                    "{{kind}} p95",
                ),
            ],
            unit="s",
        ),
        timeseries(
            lay,
            "Embedding p95 (query / document batch)",
            [
                (
                    "histogram_quantile(0.95, sum by (purpose, le) "
                    "(rate(ai_embed_seconds_bucket[5m])))",
                    "{{purpose}}",
                )
            ],
            unit="s",
        ),
        timeseries(
            lay,
            "Index lag p95 by event",
            [
                (
                    "histogram_quantile(0.95, sum by (event_type, le) "
                    "(rate(ai_index_lag_seconds_bucket[5m])))",
                    "{{event_type}}",
                )
            ],
            unit="s",
        ),
        timeseries(
            lay,
            "Index writes / s (written vs. stale)",
            [
                (
                    "sum by (collection, result) (rate(ai_index_writes_total[5m]))",
                    "{{collection}} {{result}}",
                ),
                ("sum(rate(ai_orders_skipped_total[5m]))", "orders skipped (restaurant gone)"),
            ],
            unit="ops",
            stack=True,
        ),
        timeseries(
            lay,
            "Indexer: consumed / retried per second",
            [
                (
                    f"sum by (topic, outcome) (rate(kafka_events_consumed_total{{{indexer}}}[5m]))",
                    "{{topic}} {{outcome}}",
                ),
                (
                    f"sum by (kind) (rate(kafka_event_retries_total{{{indexer}}}[5m]))",
                    "retry ({{kind}})",
                ),
            ],
            unit="ops",
        ),
    ]
    return dashboard("sfo-ai", "SmartFoodOps — AI / Retrieval", panels)


def _p(q: float, metric: str, by: str = "", where: str = "") -> str:
    """A histogram quantile over 5 minutes, optionally per label / filtered."""
    labels = f"{by}, le" if by else "le"
    selector = f"{metric}_bucket{{{where}}}" if where else f"{metric}_bucket"
    return f"histogram_quantile({q}, sum by ({labels}) (rate({selector}[5m])))"


def ai_assistant() -> dict[str, Any]:
    """The GenAI layer (docs/week5-plan.md step 7): traffic, latency against
    the §3 targets, LLM errors by type, answer quality, tokens and cost. Per
    step timings of one turn are in Jaeger (spans `assistant.<step>`, `llm
    <role>`)."""
    lay = Layout()
    failed = 'result!~"ok|rate_limited"'
    panels = [
        stat(
            lay,
            "First output p95 (chat)",
            _p(0.95, "ai_assistant_first_output_seconds"),
            unit="s",
            thresholds=[(None, "green"), (4, "orange"), (8, "red")],
            description="Message → first card or word. Target < 4 s; alert AiAnswersSlow.",
        ),
        stat(
            lay,
            "Full answer p95 (chat)",
            _p(0.95, "ai_assistant_answer_seconds", where='kind="chat"'),
            unit="s",
            thresholds=[(None, "green"), (10, "orange"), (20, "red")],
            description="Message → last word, finished answers only. Target < 10 s.",
        ),
        stat(
            lay,
            "LLM calls failing (5m)",
            f"(sum(rate(ai_llm_calls_total{{{failed}}}[5m])) or vector(0))"
            " / sum(rate(ai_llm_calls_total[5m]))",
            unit="percentunit",
            thresholds=[(None, "green"), (0.05, "orange"), (0.2, "red")],
            description="Not counting rate limits. Alert AiLlmErrors above 20%, AiLlmAuth on a "
            "rejected key.",
        ),
        stat(
            lay,
            "Rate-limited LLM calls (range)",
            'round(sum(increase(ai_llm_calls_total{result="rate_limited"}[$__range]))) '
            "or vector(0)",
            thresholds=[(None, "green"), (1, "orange")],
            description="The provider throttled us; retried, the answer just comes later.",
        ),
        stat(
            lay,
            "Made-up IDs caught (range)",
            "round(sum(increase(ai_assistant_made_up_ids_total[$__range]))) or vector(0)",
            thresholds=[(None, "green"), (1, "orange")],
            description="Dishes the model picked that weren't among its candidates: the check "
            "dropped them, the customer never saw them.",
        ),
        stat(
            lay,
            "LLM spend (range)",
            "sum(increase(ai_llm_cost_usd_total[$__range])) or vector(0)",
            unit="currencyUSD",
            description="From the price table in app/llm.py; 0 for unpriced models.",
        ),
        # --- Traffic ---
        timeseries(
            lay,
            "Assistant turns / min by how they ended",
            [("sum by (status) (rate(ai_assistant_turns_total[5m])) * 60", "{{status}}")],
            unit="short",
            stack=True,
            description="answered, refused (not food / nothing fits), degraded (search cards or "
            "code's text), interrupted (customer left), error.",
        ),
        timeseries(
            lay,
            "Questions / min by intent",
            [("sum by (intent) (rate(ai_assistant_intents_total[5m])) * 60", "{{intent}}")],
            stack=True,
        ),
        # --- Latency ---
        timeseries(
            lay,
            "Answer time p50 / p95 (targets: first 4 s, full 10 s)",
            [
                (_p(0.5, "ai_assistant_first_output_seconds"), "first output p50"),
                (_p(0.95, "ai_assistant_first_output_seconds"), "first output p95"),
                (_p(0.95, "ai_assistant_answer_seconds", "kind"), "full answer p95 ({{kind}})"),
            ],
            unit="s",
        ),
        timeseries(
            lay,
            "LLM call p95 by role and model",
            [(_p(0.95, "ai_llm_call_seconds", "role, model"), "{{role}} {{model}}")],
            unit="s",
        ),
        # --- Errors ---
        timeseries(
            lay,
            "LLM calls / min by result",
            [
                (
                    "sum by (result) (rate(ai_llm_calls_total[5m])) * 60",
                    "{{result}}",
                )
            ],
            stack=True,
            description="ok, rate_limited, unavailable (timeout / 5xx), auth (bad key), "
            "bad_request, bad_output (no valid structured answer).",
        ),
        timeseries(
            lay,
            "How picks ended / min",
            [("sum by (outcome) (rate(ai_assistant_outcomes_total[5m])) * 60", "{{outcome}}")],
            stack=True,
            description="retried (check failed once), fallback (search cards shown), no_match, "
            "nothing_found (search found nothing).",
        ),
        # --- Quality ---
        timeseries(
            lay,
            "Checks in code catching the model / min",
            [
                ("sum(rate(ai_assistant_made_up_ids_total[5m])) * 60", "made-up dish IDs"),
                (
                    'sum(rate(ai_order_explanations_total{outcome="number_guard"}[5m])) * 60',
                    "order explanation: number not in the facts",
                ),
                (
                    "sum by (kind) (rate(ai_content_check_rejections_total[5m])) * 60",
                    "content rejected: {{kind}}",
                ),
            ],
        ),
        timeseries(
            lay,
            "Order explanations and content drafts / min",
            [
                (
                    "sum by (outcome) (rate(ai_order_explanations_total[5m])) * 60",
                    "explain {{outcome}}",
                ),
                (
                    "sum by (status) (rate(ai_content_drafts_total[5m])) * 60",
                    "content {{status}}",
                ),
            ],
        ),
        # --- Cost ---
        timeseries(
            lay,
            "Tokens / min by model",
            [
                (
                    "sum by (model, kind) (rate(ai_llm_tokens_total[5m])) * 60",
                    "{{model}} {{kind}}",
                )
            ],
            stack=True,
        ),
        timeseries(
            lay,
            "Spend $ / hour by model",
            [("sum by (model) (rate(ai_llm_cost_usd_total[5m])) * 3600", "{{model}}")],
            unit="currencyUSD",
            stack=True,
        ),
    ]
    lay.new_row()
    panels.append(alerts_panel(lay))
    return dashboard(
        "sfo-ai-assistant",
        "SmartFoodOps — AI Assistant",
        panels,
        links=[
            {
                "title": "Assistant traces (Jaeger)",
                "type": "link",
                "url": "http://localhost:16686/search?service=ai-service"
                "&operation=assistant.understand",
                "targetBlank": True,
            }
        ],
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, board in {
        "service-overview": service_overview(),
        "orders-and-deliveries": business(),
        "event-pipeline": pipeline(),
        "logs": logs(),
        "ai-retrieval": ai_retrieval(),
        "ai-assistant": ai_assistant(),
    }.items():
        (OUT / f"{name}.json").write_text(json.dumps(board, indent=2) + "\n")
        print(f"wrote {name}.json ({len(board['panels'])} panels)")


if __name__ == "__main__":
    main()
