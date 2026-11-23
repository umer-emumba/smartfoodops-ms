"""pymongo's real errors against the consumer's outage check: a Mongo
outage must be waited out, not dead-lettered. sfo-events matches these by
class name (it doesn't depend on pymongo), so this catches a pymongo
upgrade that renames or moves them."""

import pytest
from pymongo.errors import (
    AutoReconnect,
    DuplicateKeyError,
    NetworkTimeout,
    OperationFailure,
    ServerSelectionTimeoutError,
    WaitQueueTimeoutError,
)
from sfo_events.consumer import is_transient


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ServerSelectionTimeoutError("No servers found"), True),
        (AutoReconnect("connection reset"), True),
        (NetworkTimeout("timed out"), True),
        (WaitQueueTimeoutError("pool exhausted"), True),
        # A rejected write is the event's fault, not an outage.
        (DuplicateKeyError("E11000 duplicate key"), False),
        (OperationFailure("bad update"), False),
    ],
)
def test_mongo_errors(exc: BaseException, transient: bool) -> None:
    assert is_transient(exc) is transient
