"""redis-py's real errors against the consumer's outage check: a Redis
outage must be waited out, not dead-lettered. sfo-events matches these by
class name (it doesn't depend on redis), so this catches a redis-py
upgrade that renames or moves them."""

import pytest
from redis.exceptions import BusyLoadingError, ConnectionError, ResponseError, TimeoutError
from sfo_events.consumer import is_transient


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ConnectionError("Connection refused"), True),
        (TimeoutError("Timeout reading from socket"), True),
        # Redis still loading its data after a restart.
        (BusyLoadingError("LOADING"), True),
        # A command error (wrong type, bad args) isn't an outage.
        (ResponseError("WRONGTYPE"), False),
    ],
)
def test_redis_errors(exc: BaseException, transient: bool) -> None:
    assert is_transient(exc) is transient
