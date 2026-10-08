from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.models import Order
from sfo_common.errors import ValidationAppError
from sfo_common.pagination import (
    build_page,
    decode_cursor,
    encode_cursor,
    paginate_newest_first,
)
from sqlalchemy import select
from sqlalchemy.dialects import postgresql


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_cursor_round_trips() -> None:
    ts = datetime(2026, 10, 8, 12, 30, 1, 123456, tzinfo=UTC)
    assert decode_cursor(encode_cursor(ts, "abc")) == (ts, "abc")


def test_garbage_cursor_is_422_not_500() -> None:
    with pytest.raises(ValidationAppError):
        decode_cursor("not-a-cursor")


def test_first_page_sorts_by_created_at_then_id() -> None:
    sql = _sql(paginate_newest_first(select(Order), Order, cursor=None, limit=20))
    assert "ORDER BY orders.created_at DESC, orders.id DESC" in sql
    assert "LIMIT" in sql
    assert "(orders.created_at, orders.id) <" not in sql


def test_next_page_resumes_after_cursor_with_tie_breaker() -> None:
    cursor = encode_cursor(datetime(2026, 10, 8, tzinfo=UTC), "some-id")
    stmt = select(Order).where(Order.status == "created")
    sql = _sql(paginate_newest_first(stmt, Order, cursor=cursor, limit=20))
    # Row-value comparison: rows sharing the cursor's created_at are
    # separated by id instead of being skipped or repeated.
    assert "(orders.created_at, orders.id) <" in sql
    # The caller's filter is kept alongside the cursor condition.
    assert "orders.status =" in sql


def _row(n: int) -> SimpleNamespace:
    return SimpleNamespace(created_at=datetime(2026, 10, 8, 0, 0, n, tzinfo=UTC), id=f"id-{n}")


def test_build_page_has_cursor_only_when_more_rows_exist() -> None:
    # paginate_newest_first fetches limit + 1 rows; the extra one only signals
    # that another page exists and is not returned.
    rows = [_row(3), _row(2), _row(1)]
    page = build_page(rows, limit=2)
    assert [r.id for r in page["items"]] == ["id-3", "id-2"]
    assert decode_cursor(page["next_cursor"]) == (rows[1].created_at, "id-2")

    last = build_page(rows[:2], limit=2)
    assert last["next_cursor"] is None
    assert build_page([], limit=2) == {"items": [], "next_cursor": None}
