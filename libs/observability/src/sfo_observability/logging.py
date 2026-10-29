"""Structured logs for every service: each log line is one JSON object with the
service name, level, `request_id` and `trace_id`, with passwords and tokens
removed. Grafana Alloy ships these lines to Loki, where Grafana can search all
services at once and jump from a line to its trace in Jaeger."""

import json
import logging
import sys
from datetime import UTC, datetime

from sfo_common.middleware import get_correlation_id

from sfo_observability.tracing import current_trace_ids

# Never let these show up in a log line, even at debug level — enforced here
# rather than trusted to each call site. See docs/architecture.md#conventions.
_REDACTED_KEYS = {
    "password",
    "hashed_password",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "jwt_secret",
    "jwt_private_key",
    "private_key",
    "card_number",
    "cvv",
}
_REDACTED_VALUE = "***REDACTED***"


def _sanitize(obj: object) -> object:
    if isinstance(obj, dict):
        return {
            k: (_REDACTED_VALUE if k.lower() in _REDACTED_KEYS else _sanitize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


class _JsonFormatter(logging.Formatter):
    """Turns each log record into one JSON line, adding the request and trace
    IDs that are active when it's logged — nothing to pass in by hand."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
            # The request's X-Request-ID — the same value in every service a
            # request touches, so `grep <request_id>` across all services'
            # logs shows its whole path. Null outside a request (startup,
            # jobs).
            "request_id": get_correlation_id() or None,
        }
        # The OpenTelemetry trace/span this line was written in — also set in
        # Temporal activities, Kafka consumers and Celery tasks, where there
        # is no request ID. Grafana links a log line's trace_id to the trace
        # in Jaeger (week3-plan W3-D13).
        trace_id, span_id = current_trace_ids()
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload["extra"] = _sanitize(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Structured JSON logs on stdout with the request's trace ID attached,
    and sensitive fields stripped before anything is written. Call once at
    service startup (module import time, after uvicorn has set up its own
    logging, so the overrides below win).

    stdout, not files: the container runtime captures it (rotated, see
    docker-compose.yml's logging options) and a collector ships it to one
    searchable place — the twelve-factor approach; files inside containers
    vanish with the container and have to be rotated per pod."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(service_name))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # uvicorn installs its own plain-text handlers; route its logs through
    # the JSON handler above instead. Its access log is turned off entirely —
    # sfo_common.middleware.CorrelationIdMiddleware writes a JSON access line
    # per request that carries the trace ID (uvicorn's can't).
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
