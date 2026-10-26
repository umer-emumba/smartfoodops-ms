"""Orders inserted straight into the test database, in any status."""

from datetime import UTC, datetime
from decimal import Decimal

from app.models import Order, OrderItem, OrderStatus, OrderStatusHistory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CUSTOMER = "u-customer"
OWNER = "u-owner"
RESTAURANT = "r-1"


async def insert_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: OrderStatus = OrderStatus.CREATED,
    customer_id: str = CUSTOMER,
    restaurant_id: str = RESTAURANT,
    restaurant_admin_id: str = OWNER,
    subtotal: Decimal = Decimal("1000.00"),
    created_at: datetime | None = None,
) -> Order:
    async with factory() as session:
        order = Order(
            customer_id=customer_id,
            restaurant_id=restaurant_id,
            restaurant_admin_id=restaurant_admin_id,
            restaurant_lat=33.68,
            restaurant_lng=73.04,
            delivery_address_text="1 Test St",
            delivery_lat=33.70,
            delivery_lng=73.05,
            subtotal=subtotal,
            total_amount=subtotal,
            status=status,
            created_at=created_at or datetime.now(UTC),
        )
        order.items = [
            OrderItem(
                menu_item_id="m-1",
                item_name_snapshot="Biryani",
                quantity=2,
                unit_price=subtotal / 2,
            )
        ]
        order.status_history = [OrderStatusHistory(from_status=None, to_status=OrderStatus.CREATED)]
        session.add(order)
        await session.commit()
        return order
