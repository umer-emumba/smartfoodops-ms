"""What content is written from: the restaurant's own data, read from the
search index (Week 4) — menu items, reviews and delivered orders are all
there, kept current by the indexer from restaurant- and order-events within
seconds. So ai-worker needs no call to another service.

`Facts` is the input to every kind; reading it is plain code, no model."""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from qdrant_client.http import models as m

from app import qdrant
from app.config import settings
from app.store import MENU_ITEMS, ORDER_HISTORY, RESTAURANTS, REVIEWS, name

_PAGE = 256


@dataclass
class Review:
    order_id: str
    rating: int
    text: str
    created_at: str


@dataclass
class Facts:
    restaurant: dict[str, Any]
    menu: list[dict[str, Any]]  # available, not deleted
    reviews: list[Review] = field(default_factory=list)  # newest first
    ordered: Counter[str] = field(default_factory=Counter)  # menu_item_id → quantity

    @property
    def rating(self) -> tuple[float | None, int]:
        """Average rating and count, over the reviews read."""
        if not self.reviews:
            return None, 0
        return round(sum(r.rating for r in self.reviews) / len(self.reviews), 2), len(self.reviews)


def _restaurant_filter(restaurant_id: str, *extra: m.Condition) -> m.Filter:
    return m.Filter(
        must=[
            m.FieldCondition(key="restaurant_id", match=m.MatchValue(value=restaurant_id)),
            *extra,
        ],
        must_not=[m.FieldCondition(key="deleted", match=m.MatchValue(value=True))],
    )


async def _scroll(collection: str, flt: m.Filter, limit: int | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = None
    while True:
        points, offset = await qdrant.client.scroll(
            name(collection), scroll_filter=flt, limit=_PAGE, offset=offset, with_payload=True
        )
        out += [dict(p.payload or {}) for p in points]
        if offset is None or (limit is not None and len(out) >= limit):
            return out


async def restaurant(restaurant_id: str) -> dict[str, Any] | None:
    rows = await _scroll(RESTAURANTS, _restaurant_filter(restaurant_id))
    return rows[0] if rows else None


async def menu(restaurant_id: str) -> list[dict[str, Any]]:
    rows = await _scroll(MENU_ITEMS, _restaurant_filter(restaurant_id))
    return sorted(
        (r for r in rows if r.get("is_available")),
        key=lambda r: (r.get("category") or "", r["name"]),
    )


async def reviews(restaurant_id: str, limit: int) -> list[Review]:
    """Whole reviews, newest first: chunks of one review (Week 4 splits long
    ones) are joined back in order, without the "Review of X, n/5:" prefix."""
    rows = await _scroll(REVIEWS, _restaurant_filter(restaurant_id))
    by_order: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_order.setdefault(str(r["order_id"]), []).append(r)
    out = []
    for order_id, chunks in by_order.items():
        chunks.sort(key=lambda c: c.get("chunk", 0))
        text = " ".join(str(c["text"]).split(": ", 1)[-1] for c in chunks)
        first = chunks[0]
        out.append(Review(order_id, int(first["rating"]), text, str(first.get("created_at", ""))))
    out.sort(key=lambda r: r.created_at, reverse=True)
    return out[:limit]


async def ordered(restaurant_id: str) -> Counter[str]:
    """How many of each dish were ordered (delivered orders in the index)."""
    counts: Counter[str] = Counter()
    for o in await _scroll(
        ORDER_HISTORY,
        _restaurant_filter(
            restaurant_id, m.FieldCondition(key="doc_type", match=m.MatchValue(value="order"))
        ),
    ):
        for item_id, qty in zip(o.get("menu_item_ids", []), o.get("quantities", []), strict=False):
            counts[str(item_id)] += int(qty or 1)
    return counts


async def facts(restaurant_id: str, *, with_reviews: bool, with_orders: bool) -> Facts:
    found = await restaurant(restaurant_id)
    if found is None:
        raise LookupError(f"restaurant {restaurant_id} isn't in the index")
    return Facts(
        restaurant=found,
        menu=await menu(restaurant_id),
        reviews=await reviews(restaurant_id, settings.content_max_reviews) if with_reviews else [],
        ordered=await ordered(restaurant_id) if with_orders else Counter(),
    )
