"""Generates the Grafana dashboards in infra/grafana/dashboards/ (provisioned
at Grafana's start-up). Edit here, then run:

    uv run python infra/grafana/build_dashboards.py

Four dashboards (docs/week3-plan.md step 5):
- Service overview: requests, errors and latency per service (RED).
- Orders & deliveries: the business flow — orders, outcomes, restaurant
  responses, delivery time, sagas, riders, live tracking.
- Event pipeline: outbox → Kafka → consumers → Celery → notifications, plus
  Temporal activities and RabbitMQ.
- Logs: every container's logs from Loki, filterable, with trace links.
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
                )
            ],
            unit="ops",
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


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, board in {
        "service-overview": service_overview(),
        "orders-and-deliveries": business(),
        "event-pipeline": pipeline(),
        "logs": logs(),
    }.items():
        (OUT / f"{name}.json").write_text(json.dumps(board, indent=2) + "\n")
        print(f"wrote {name}.json ({len(board['panels'])} panels)")


if __name__ == "__main__":
    main()
