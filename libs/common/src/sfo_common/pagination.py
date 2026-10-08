import base64
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, tuple_

from sfo_common.errors import ValidationAppError

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Cursor-based (keyset) pagination envelope — used on all list endpoints
    instead of offset/limit, which degrades on large tables and skips/repeats
    rows when new ones are inserted between page requests. See
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


def paginate_newest_first(stmt: Select, model: Any, *, cursor: str | None, limit: int) -> Select:
    """Orders by (created_at DESC, id DESC), resumes after `cursor`, and
    fetches one extra row so build_page can tell whether another page exists.

    `id` is the tie-breaker: created_at alone isn't unique, and a non-unique
    sort key makes keyset pagination skip or repeat rows that share a
    timestamp. created_at never changes after insert, so a row can't move
    between pages while a client is paging. The caller's filters must be
    applied to `stmt` identically on every page.
    """
    if cursor is not None:
        created_at, id_ = decode_cursor(cursor)
        stmt = stmt.where(tuple_(model.created_at, model.id) < (created_at, id_))
    return stmt.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)


def build_page(rows: list[Any], limit: int) -> dict[str, Any]:
    """Pairs with paginate_newest_first. Returns a plain dict so the route's
    `response_model=Page[XResponse]` validates the ORM rows itself, the same
    way a `list[XResponse]` response_model does."""
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more else None
    return {"items": rows, "next_cursor": next_cursor}
