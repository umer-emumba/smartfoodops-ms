"""Server-Sent Events wire format (https://html.spec.whatwg.org/#server-sent-events)."""

import json
from typing import Any

# A comment line: ignored by EventSource, keeps the connection busy.
HEARTBEAT = ": heartbeat\n\n"

HEADERS = {
    "Cache-Control": "no-cache",
    # Tell any nginx-style proxy in front not to buffer the stream.
    "X-Accel-Buffering": "no",
}


def event(name: str, data: dict[str, Any] | list[Any]) -> str:
    # JSON never contains a raw newline, so it fits one `data:` line.
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
