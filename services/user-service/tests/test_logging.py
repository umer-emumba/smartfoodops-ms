import json
import logging

from sfo_common.middleware import _correlation_id
from sfo_observability import configure_logging


def _format(record: logging.LogRecord) -> dict:
    handler = logging.getLogger().handlers[0]
    return json.loads(handler.format(record))


def test_log_lines_carry_trace_id_and_redact_secrets() -> None:
    configure_logging("user-service")
    token = _correlation_id.set("trace-xyz")
    try:
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello", None, None)
        record.extra_fields = {"password": "hunter2", "count": 3}
        line = _format(record)
    finally:
        _correlation_id.reset(token)
    assert line["trace_id"] == "trace-xyz"
    assert line["service"] == "user-service"
    assert line["extra"] == {"password": "***REDACTED***", "count": 3}


def test_no_trace_id_outside_a_request() -> None:
    configure_logging("user-service")
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "startup", None, None)
    assert _format(record)["trace_id"] is None


def test_uvicorn_logs_go_through_json_and_plain_access_log_is_off() -> None:
    configure_logging("user-service")
    assert logging.getLogger("uvicorn.error").handlers == []
    assert logging.getLogger("uvicorn.error").propagate
    assert logging.getLogger("uvicorn.access").disabled
