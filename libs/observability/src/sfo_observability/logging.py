import json
import logging
import sys
from datetime import UTC, datetime

from sfo_common.middleware import get_correlation_id

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
    "x-bootstrap-secret",
    "admin_bootstrap_secret",
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
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload["extra"] = _sanitize(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Structured JSON logs with the request's correlation ID attached, and
    sensitive fields stripped before anything hits stdout. Call once at
    service startup."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(service_name))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
