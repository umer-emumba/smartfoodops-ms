"""What gets embedded (docs/week4-plan.md step 3): one text document per
entity, plus the payload search filters on. Built from event payloads (and
the backfill's identical exports), never by calling other services.

Each text starts with its context — restaurant, cuisine, category, city — so
a document still makes sense on its own when it's retrieved without its
neighbours ("Chicken Biryani" alone says nothing about where it's from).

Point IDs are uuid5 of the entity, so the same entity always lands on the
same point and every write is an overwrite, never a duplicate."""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sfo_events.payloads import (
    MenuItemUpsertedPayload,
    OrderLine,
    RestaurantUpsertedPayload,
    ReviewSubmittedPayload,
)

_NAMESPACE = uuid.UUID("0b6c2f7e-6a43-4d8e-9a21-3c5e8f1d7b90")
SPICE = {0: "not spicy", 1: "mildly spicy", 2: "spicy", 3: "very spicy"}

# Review chunking: about 300 tokens a chunk with ~50 tokens shared between
# neighbours, so a sentence cut at a boundary is still whole in one of them.
# Counted in words (≈0.75 words a token for English) — exact token counts
# aren't needed to stay well inside bge-small's 512-token window.
CHUNK_WORDS = 220
OVERLAP_WORDS = 40


def point_id(kind: str, key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{kind}:{key}"))


@dataclass(frozen=True)
class Doc:
    id: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


def _location(lat: float, lng: float) -> dict[str, float]:
    return {"lat": lat, "lon": lng}


def menu_item(p: MenuItemUpsertedPayload) -> Doc:
    tags = ", ".join(t.replace("_", "-") for t in p.dietary_tags) or "no dietary tags"
    calories = f" About {p.calories} kcal." if p.calories is not None else ""
    text = (
        f"{p.name} — {p.category_name} at {p.restaurant_name} "
        f"({p.cuisine_type or 'restaurant'}, {p.city}). {p.description} "
        f"{SPICE.get(p.spice_level, 'spicy').capitalize()}; {tags}.{calories} "
        f"PKR {p.price:.0f}."
    )
    return Doc(
        point_id("menu_item", p.menu_item_id),
        text,
        {
            "doc_type": "menu_item",
            "menu_item_id": p.menu_item_id,
            "restaurant_id": p.restaurant_id,
            "restaurant_name": p.restaurant_name,
            "restaurant_status": p.restaurant_status,
            "cuisine": p.cuisine_type,
            "city": p.city,
            "location": _location(p.lat, p.lng),
            "category": p.category_name,
            "name": p.name,
            "description": p.description,
            "price": float(p.price),
            "dietary_tags": p.dietary_tags,
            "spice_level": p.spice_level,
            "calories": p.calories,
            "is_available": p.is_available,
            "in_stock": p.in_stock,
            "deleted": False,
            "source_version": p.source_version,
            "text": text,
        },
    )


def menu_item_tombstone(menu_item_id: str, restaurant_id: str, version: int) -> Doc:
    """Kept instead of deleting the point: a late, older upsert of the item
    then loses the version check rather than bringing it back."""
    return Doc(
        point_id("menu_item", menu_item_id),
        "",
        {
            "doc_type": "menu_item",
            "menu_item_id": menu_item_id,
            "restaurant_id": restaurant_id,
            "deleted": True,
            "source_version": version,
        },
    )


def restaurant(p: RestaurantUpsertedPayload) -> Doc:
    text = f"{p.name} — {p.cuisine_type or 'restaurant'} in {p.city} ({p.address}). {p.description}"
    return Doc(
        point_id("restaurant", p.restaurant_id),
        text,
        {
            "doc_type": "restaurant",
            "restaurant_id": p.restaurant_id,
            "name": p.name,
            "restaurant_status": p.status,
            "cuisine": p.cuisine_type,
            "city": p.city,
            "address": p.address,
            "location": _location(p.lat, p.lng),
            "description": p.description,
            "is_accepting_orders": p.is_accepting_orders,
            "avg_prep_time_minutes": p.avg_prep_time_minutes,
            "avg_rating": p.avg_rating,
            "review_count": p.review_count,
            "deleted": False,
            "source_version": p.source_version,
            "text": text,
        },
    )


def chunk_words(text: str, size: int = CHUNK_WORDS, overlap: int = OVERLAP_WORDS) -> list[str]:
    words = text.split()
    if len(words) <= size:
        return [" ".join(words)]
    step = size - overlap
    chunks = []
    for start in range(0, len(words), step):
        chunks.append(" ".join(words[start : start + size]))
        if start + size >= len(words):
            break
    return chunks


def review(p: ReviewSubmittedPayload, restaurant_payload: dict[str, Any] | None) -> list[Doc]:
    """One Doc per chunk. Location, city and status come from the
    restaurant's indexed point (None if it isn't indexed yet)."""
    where = restaurant_payload or {}
    chunks = chunk_words(p.text)
    docs = []
    for n, chunk in enumerate(chunks):
        text = f"Review of {p.restaurant_name}, {p.rating}/5: {chunk}"
        docs.append(
            Doc(
                point_id("review", f"{p.order_id}:{n}"),
                text,
                {
                    "doc_type": "review",
                    "order_id": p.order_id,
                    "restaurant_id": p.restaurant_id,
                    "restaurant_name": p.restaurant_name,
                    "restaurant_status": where.get("restaurant_status"),
                    "cuisine": where.get("cuisine"),
                    "city": where.get("city"),
                    "location": where.get("location"),
                    "rating": p.rating,
                    "item_ids": p.item_ids,
                    "created_at": p.created_at.isoformat(),
                    "chunk": n,
                    "chunks": len(chunks),
                    "deleted": False,
                    "source_version": p.source_version,
                    "text": text,
                },
            )
        )
    return docs


@dataclass(frozen=True)
class DeliveredOrder:
    order_id: str
    customer_id: str
    restaurant_id: str
    total_amount: float
    delivered_at: datetime
    items: list[OrderLine]


def order(o: DeliveredOrder, restaurant_payload: dict[str, Any]) -> Doc:
    lines = ", ".join(f"{line.quantity}× {line.name}" for line in o.items)
    name = restaurant_payload.get("name", "")
    cuisine = restaurant_payload.get("cuisine") or "restaurant"
    text = (
        f"Order from {name} ({cuisine}) on {o.delivered_at:%d %B %Y}: {lines}. "
        f"Total PKR {o.total_amount:.0f}."
    )
    return Doc(
        point_id("order", o.order_id),
        text,
        {
            "doc_type": "order",
            "customer_id": o.customer_id,
            "order_id": o.order_id,
            "restaurant_id": o.restaurant_id,
            "restaurant_name": name,
            "cuisine": restaurant_payload.get("cuisine"),
            "items": [line.name for line in o.items],
            "quantities": [line.quantity for line in o.items],
            "menu_item_ids": [line.menu_item_id for line in o.items],
            "total_amount": o.total_amount,
            "delivered_at": o.delivered_at.isoformat(),
            "text": text,
        },
    )


def customer_summary(customer_id: str, orders: list[dict[str, Any]]) -> Doc:
    """The rolling summary of a customer's history (their order points'
    payloads): what they order, from where, and how much they spend — the
    profile Week 5's recommendations start from."""
    orders = sorted(orders, key=lambda o: o["delivered_at"], reverse=True)
    dishes: Counter[str] = Counter()
    for o in orders:
        for dish, quantity in zip(o["items"], o["quantities"], strict=True):
            dishes[dish] += quantity
    cuisines = Counter(o["cuisine"] for o in orders if o.get("cuisine"))
    places = Counter(o["restaurant_name"] for o in orders if o.get("restaurant_name"))
    spend = sum(o["total_amount"] for o in orders) / len(orders)

    def top(counter: Counter[str], n: int) -> str:
        return ", ".join(f"{k} ({v})" for k, v in counter.most_common(n)) or "none"

    text = (
        f"Customer order history: {len(orders)} delivered orders, the latest on "
        f"{orders[0]['delivered_at'][:10]}. Favourite cuisines: {top(cuisines, 3)}. "
        f"Most ordered dishes: {top(dishes, 5)}. Usual restaurants: {top(places, 3)}. "
        f"Average spend PKR {spend:.0f} an order."
    )
    return Doc(
        point_id("customer_summary", customer_id),
        text,
        {
            "doc_type": "customer_summary",
            "customer_id": customer_id,
            "order_count": len(orders),
            "last_order_at": orders[0]["delivered_at"],
            "top_cuisines": [k for k, _ in cuisines.most_common(3)],
            "top_dishes": [k for k, _ in dishes.most_common(5)],
            "average_spend": round(spend, 2),
            "text": text,
        },
    )
