"""Prometheus metrics (docs/week3-plan.md W3-D14).

Every process — API or Celery worker — serves its metrics on an internal
port (`METRICS_PORT`, 9100 in docker compose), never on the API port: the
gateway's own port is published, and scrape endpoints shouldn't be public.
Off when `METRICS_PORT` isn't set (unit tests, a bare `uvicorn`); the
metrics are still recorded, just not served.

- `instrument_metrics(app)`: RED metrics per request — rate, errors (by
  status), duration — labelled by route *template* (`/v1/orders/{order_id}`),
  so IDs in paths can't create a series per order.
- `instrument_celery_metrics()`: tasks by outcome, and duration.

Domain metrics live next to the code they measure (sfo_events for the
outbox and consumers, each service for its own)."""

import os
import time
from typing import Any

from prometheus_client import Counter, Histogram, start_http_server
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sfo_observability.tracing import QUIET_PATHS

_server_started = False

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration (until the response is fully sent)",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
CELERY_TASKS = Counter(
    "celery_tasks_total", "Celery task runs by outcome", ["task", "outcome"]
)  # outcome: succeeded | retried | failed
CELERY_DURATION = Histogram(
    "celery_task_duration_seconds",
    "Celery task run time",
    ["task"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

# Live SSE streams stay open for minutes; their duration says nothing about
# latency, so they're counted but kept out of the histogram.
_STREAMING_PREFIX = "/v1/tracking/"


def start_metrics_server() -> None:
    """Idempotent; a no-op without METRICS_PORT."""
    global _server_started
    port = os.environ.get("METRICS_PORT")
    if _server_started or not port:
        return
    start_http_server(int(port))
    _server_started = True


def instrument_metrics(app: Any) -> None:
    app.add_middleware(_RedMetrics)
    start_metrics_server()


class _RedMetrics:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith(QUIET_PATHS):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500  # if the app raises before responding

        async def send_and_record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_record_status)
        finally:
            # FastAPI puts the matched route in the scope while routing.
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope["method"]
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            if not path.startswith(_STREAMING_PREFIX):
                HTTP_DURATION.labels(method, route).observe(time.perf_counter() - started)


def instrument_celery_metrics() -> None:
    """Task outcomes and durations from Celery's signals, for whichever
    worker runs in this process; serves them too (worker processes have no
    API to start the server)."""
    from celery.signals import task_failure, task_postrun, task_prerun, task_retry, task_success

    started: dict[str, float] = {}

    @task_prerun.connect(weak=False)
    def _prerun(task_id: str, **_: Any) -> None:
        started[task_id] = time.perf_counter()

    @task_postrun.connect(weak=False)
    def _postrun(task_id: str, task: Any, **_: Any) -> None:
        begun = started.pop(task_id, None)
        if begun is not None:
            CELERY_DURATION.labels(task.name).observe(time.perf_counter() - begun)

    @task_success.connect(weak=False)
    def _success(sender: Any, **_: Any) -> None:
        CELERY_TASKS.labels(sender.name, "succeeded").inc()

    @task_retry.connect(weak=False)
    def _retry(sender: Any, **_: Any) -> None:
        CELERY_TASKS.labels(sender.name, "retried").inc()

    @task_failure.connect(weak=False)
    def _failure(sender: Any, **_: Any) -> None:
        CELERY_TASKS.labels(sender.name, "failed").inc()

    start_metrics_server()
