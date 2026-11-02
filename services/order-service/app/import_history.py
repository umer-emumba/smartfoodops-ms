"""Imports past, already-delivered orders — demo data only. Run inside the
order-service container, not over HTTP:

    docker compose exec -T order-service python -m app.import_history < orders.json

scripts/seed.py builds the JSON (a list of ImportedOrder) from
scripts/fixtures/catalog.json: the demo customer's order history, and the
orders behind the imported reviews (restaurant-service's
app/import_reviews.py). Search and recommendations need months of history,
which the real saga can't produce: it takes minutes per order and stamps
everything "now".

Each order is written as delivered, with the usual pricing (app/pricing.py)
and a status trail spaced like a normal delivery, starting at `placed_at`.
No saga runs and no events are written, so payments, deliveries and
analytics don't know about these orders. Re-runnable: an order ID that
already exists is skipped.

Exit codes: 0 done, 1 invalid input."""

import asyncio
import sys
from datetime import timedelta
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, Field, TypeAdapter, ValidationError
from sfo_contracts.activities import GeoPoint
from sfo_contracts.workflows import PricingConfig

from app import pricing
from app.config import settings
from app.db import SessionFactory, engine
from app.models import Order, OrderItem, OrderStatus, OrderStatusHistory

# Minutes after placed_at at which each status was reached.
_TIMELINE = [
    (None, OrderStatus.CREATED, 0),
    (OrderStatus.CREATED, OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE, 0),
    (OrderStatus.AWAITING_RESTAURANT_ACCEPTANCE, OrderStatus.CONFIRMED, 2),
    (OrderStatus.CONFIRMED, OrderStatus.ASSIGNED, 5),
    (OrderStatus.ASSIGNED, OrderStatus.PICKED_UP, 25),
    (OrderStatus.PICKED_UP, OrderStatus.DELIVERED, 45),
]


class ImportedItem(BaseModel):
    menu_item_id: str
    name: str
    quantity: int = Field(ge=1, le=50)
    unit_price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class ImportedOrder(BaseModel):
    id: str
    customer_id: str
    restaurant_id: str
    restaurant_admin_id: str
    restaurant_lat: float
    restaurant_lng: float
    delivery_address_text: str
    delivery_lat: float
    delivery_lng: float
    items: list[ImportedItem] = Field(min_length=1)
    placed_at: AwareDatetime


_orders_json = TypeAdapter(list[ImportedOrder])


def _build(data: ImportedOrder, config: PricingConfig) -> Order:
    at = {status: data.placed_at + timedelta(minutes=m) for _, status, m in _TIMELINE}
    subtotal = sum((i.unit_price * i.quantity for i in data.items), Decimal("0"))
    tax = pricing.tax(subtotal, Decimal("0"), config)
    fee = pricing.delivery_fee(
        GeoPoint(lat=data.restaurant_lat, lng=data.restaurant_lng),
        GeoPoint(lat=data.delivery_lat, lng=data.delivery_lng),
        config,
    )
    return Order(
        id=data.id,
        customer_id=data.customer_id,
        restaurant_id=data.restaurant_id,
        restaurant_admin_id=data.restaurant_admin_id,
        restaurant_lat=data.restaurant_lat,
        restaurant_lng=data.restaurant_lng,
        status=OrderStatus.DELIVERED,
        delivery_address_text=data.delivery_address_text,
        delivery_lat=data.delivery_lat,
        delivery_lng=data.delivery_lng,
        subtotal=subtotal,
        tax_amount=tax,
        delivery_fee=fee,
        total_amount=subtotal + tax + fee,
        created_at=data.placed_at,
        updated_at=at[OrderStatus.DELIVERED],
        confirmed_at=at[OrderStatus.CONFIRMED],
        assigned_at=at[OrderStatus.ASSIGNED],
        picked_up_at=at[OrderStatus.PICKED_UP],
        delivered_at=at[OrderStatus.DELIVERED],
        items=[
            OrderItem(
                menu_item_id=i.menu_item_id,
                item_name_snapshot=i.name,
                quantity=i.quantity,
                unit_price=i.unit_price,
            )
            for i in data.items
        ],
        status_history=[
            OrderStatusHistory(
                from_status=from_status,
                to_status=to_status,
                reason="imported",
                created_at=at[to_status],
            )
            for from_status, to_status, _ in _TIMELINE
        ],
    )


async def import_history(orders: list[ImportedOrder]) -> int:
    """Returns how many were new."""
    config = PricingConfig(
        tax_rate=settings.tax_rate,
        base_delivery_fee=settings.base_delivery_fee,
        per_km_fee=settings.per_km_fee,
    )
    async with SessionFactory() as session:
        inserted = 0
        for data in orders:
            if await session.get(Order, data.id) is not None:
                continue
            session.add(_build(data, config))
            inserted += 1
        await session.commit()
    return inserted


async def _run(orders: list[ImportedOrder]) -> int:
    try:
        inserted = await import_history(orders)
    finally:
        await engine.dispose()
    print(f"Imported {inserted} new orders ({len(orders) - inserted} already there)")
    return 0


def main() -> int:
    try:
        orders = _orders_json.validate_json(sys.stdin.buffer.read())
    except ValidationError as exc:
        print(exc, file=sys.stderr)
        return 1
    return asyncio.run(_run(orders))


if __name__ == "__main__":
    raise SystemExit(main())
