"""restaurant-events (docs/week4-plan.md step 2): the outbox rows for
catalog changes, written in the same transaction as the change. The
restaurant-worker's relay publishes them, keyed by restaurant_id.

Every transaction that writes events first calls `next_version`. It bumps
`restaurants.catalog_version` and that UPDATE locks the restaurant row, so
the version rises in commit order and all events of the transaction carry
it. Call it before changing menu items: the order saga's reserve locks the
restaurant first and then items, and the same order here avoids deadlocks.

Payloads are full snapshots (sfo_events.payloads), so the indexer never
calls back here."""

from decimal import Decimal

from sfo_common.middleware import get_correlation_id
from sfo_events import EventType, new_outbox_row
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models import MenuCategory, MenuItem, OutboxEvent, Restaurant, Review

# A change to these restaurant fields changes every menu item's search text
# or filters, so all items are re-sent.
ITEM_FIELDS_FROM_RESTAURANT = frozenset({"name", "cuisine_type", "city", "lat", "lng"})
_CENTS = Decimal("0.01")


async def next_version(session: AsyncSession, restaurant: Restaurant | str) -> int:
    """Bumps and returns the restaurant's catalog_version. Given the ORM
    restaurant, its rating fields are refreshed too (read under the row lock,
    so the snapshot can't carry an older rating than a review committed
    before it)."""
    restaurant_id = restaurant if isinstance(restaurant, str) else restaurant.id
    row = (
        await session.execute(
            update(Restaurant)
            .where(Restaurant.id == restaurant_id)
            .values(catalog_version=Restaurant.catalog_version + 1)
            .returning(Restaurant.catalog_version, Restaurant.rating_sum, Restaurant.review_count)
        )
    ).one()
    if isinstance(restaurant, Restaurant):
        set_committed_value(restaurant, "catalog_version", row.catalog_version)
        set_committed_value(restaurant, "rating_sum", row.rating_sum)
        set_committed_value(restaurant, "review_count", row.review_count)
    return int(row.catalog_version)


def _add(session: AsyncSession, restaurant_id: str, event_type: EventType, payload: dict) -> None:
    session.add(
        new_outbox_row(
            OutboxEvent,
            aggregate_id=restaurant_id,
            event_type=event_type,
            payload=payload,
            # The request's X-Request-ID; none in saga activities.
            correlation_id=get_correlation_id() or None,
        )
    )


def restaurant_upserted(session: AsyncSession, restaurant: Restaurant, version: int) -> None:
    _add(
        session,
        restaurant.id,
        EventType.RESTAURANT_UPSERTED,
        {
            "restaurant_id": restaurant.id,
            "name": restaurant.name,
            "description": restaurant.description,
            "city": restaurant.city,
            "address": restaurant.address,
            "lat": restaurant.lat,
            "lng": restaurant.lng,
            "cuisine_type": restaurant.cuisine_type,
            "status": restaurant.status,
            "is_accepting_orders": restaurant.is_accepting_orders,
            "avg_prep_time_minutes": restaurant.avg_prep_time_minutes,
            "avg_rating": restaurant.avg_rating,
            "review_count": restaurant.review_count,
            "source_version": version,
        },
    )


def restaurant_status_changed(
    session: AsyncSession,
    restaurant_id: str,
    from_status: str,
    to_status: str,
    reason: str | None,
    version: int,
) -> None:
    _add(
        session,
        restaurant_id,
        EventType.RESTAURANT_STATUS_CHANGED,
        {
            "restaurant_id": restaurant_id,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "source_version": version,
        },
    )


async def menu_items_upserted(
    session: AsyncSession, restaurant: Restaurant, items: list[MenuItem], version: int
) -> None:
    """One MenuItemUpserted per item, with its category's name."""
    category_ids = {item.category_id for item in items}
    rows = await session.execute(
        select(MenuCategory.id, MenuCategory.name).where(MenuCategory.id.in_(category_ids))
    )
    names: dict[str, str] = {row.id: row.name for row in rows}
    for item in items:
        _add(
            session,
            restaurant.id,
            EventType.MENU_ITEM_UPSERTED,
            {
                "menu_item_id": item.id,
                "restaurant_id": restaurant.id,
                "restaurant_name": restaurant.name,
                "restaurant_status": restaurant.status,
                "cuisine_type": restaurant.cuisine_type,
                "city": restaurant.city,
                "lat": restaurant.lat,
                "lng": restaurant.lng,
                "category_id": item.category_id,
                "category_name": names.get(item.category_id, ""),
                "name": item.name,
                "description": item.description,
                # Fresh from a request it's e.g. Decimal("720"); stored, "720.00".
                "price": item.price.quantize(_CENTS),
                "dietary_tags": list(item.dietary_tags),
                "spice_level": item.spice_level,
                "calories": item.calories,
                "is_available": item.is_available,
                "in_stock": item.stock_count is None or item.stock_count > 0,
                "source_version": version,
            },
        )


def menu_item_deleted(session: AsyncSession, item: MenuItem, version: int) -> None:
    _add(
        session,
        item.restaurant_id,
        EventType.MENU_ITEM_DELETED,
        {"menu_item_id": item.id, "restaurant_id": item.restaurant_id, "source_version": version},
    )


def review_submitted(
    session: AsyncSession, restaurant: Restaurant, review: Review, version: int
) -> None:
    """After the review's aggregate bump: `restaurant`'s rating fields must
    already include it (repository.add_review sets them)."""
    _add(
        session,
        restaurant.id,
        EventType.REVIEW_SUBMITTED,
        {
            "order_id": review.order_id,
            "restaurant_id": restaurant.id,
            "restaurant_name": restaurant.name,
            "rating": review.rating,
            "text": review.text,
            "item_ids": list(review.item_ids),
            "created_at": review.created_at,
            "avg_rating": restaurant.avg_rating,
            "review_count": restaurant.review_count,
            "source_version": version,
        },
    )
