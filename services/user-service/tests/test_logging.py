import json
import logging

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from sfo_common.middleware import _correlation_id
from sfo_observability import configure_logging


def _format(record: logging.LogRecord) -> dict:
    handler = logging.getLogger().handlers[0]
    return json.loads(handler.format(record))


def test_log_lines_carry_request_id_and_redact_secrets() -> None:
    configure_logging("user-service")
    token = _correlation_id.set("trace-xyz")
    try:
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello", None, None)
        record.extra_fields = {"password": "hunter2", "count": 3}
        line = _format(record)
    finally:
        _correlation_id.reset(token)
    assert line["request_id"] == "trace-xyz"
    # No active span (tracing is off in tests), so no OTel IDs.
    assert "trace_id" not in line
    assert line["service"] == "user-service"
    assert line["extra"] == {"password": "***REDACTED***", "count": 3}


def test_no_request_id_outside_a_request() -> None:
    configure_logging("user-service")
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "startup", None, None)
    assert _format(record)["request_id"] is None


def test_uvicorn_logs_go_through_json_and_plain_access_log_is_off() -> None:
    configure_logging("user-service")
    assert logging.getLogger("uvicorn.error").handlers == []
    assert logging.getLogger("uvicorn.error").propagate
    assert logging.getLogger("uvicorn.access").disabled


def test_log_lines_carry_the_active_span() -> None:
    configure_logging("user-service")
    span = TracerProvider().get_tracer("test").start_span("work")
    with trace.use_span(span, end_on_exit=True):
        line = _format(logging.LogRecord("app", logging.INFO, __file__, 1, "x", None, None))
    context = span.get_span_context()
    assert line["trace_id"] == format(context.trace_id, "032x")
    assert line["span_id"] == format(context.span_id, "016x")
