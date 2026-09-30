import base64
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

from sfo_common.errors import ValidationAppError

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Cursor-based (keyset) pagination envelope — used on all list endpoints
    instead of offset/limit, which degrades on large tables. See
    docs/architecture.md#conventions.
    """

    items: list[T]
    next_cursor: str | None = None


def encode_cursor(created_at: datetime, id_: str) -> str:
    raw = f"{created_at.isoformat()}|{id_}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Raises ValidationAppError (422) on a malformed cursor — it's client
    input, so a bad one is the client's error, not a 500."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at_str, id_ = raw.split("|", 1)
        return datetime.fromisoformat(created_at_str), id_
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationAppError("Invalid pagination cursor") from exc
