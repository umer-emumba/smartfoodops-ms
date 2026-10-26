from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sfo_common.money import MoneyAmount
from sfo_common.schemas import EntityId, RequestModel
from sfo_contracts.activities import PaymentMethod

# One order can't hold more distinct lines than this — bounds the quote call to
# restaurant-service and the rows written per order.
MAX_ORDER_LINES = 50


class OrderItemCreate(RequestModel):
    """Only *what* and *how many* — name and price are looked up server-side
    from restaurant-service (app/restaurant_client.py), never trusted from the
    client."""

    menu_item_id: EntityId
    quantity: int = Field(gt=0, le=50)
    special_instructions: str | None = Field(default=None, max_length=500)


class OrderCreate(RequestModel):
    restaurant_id: EntityId
    delivery_address_text: str = Field(min_length=1, max_length=500)
    delivery_lat: float = Field(ge=-90, le=90)
    delivery_lng: float = Field(ge=-180, le=180)
    special_instructions: str | None = Field(default=None, max_length=500)
    promo_code: str | None = Field(default=None, min_length=1, max_length=50)
    items: list[OrderItemCreate] = Field(min_length=1, max_length=MAX_ORDER_LINES)
    # Simulated card (week2-plan step 3): `card_simulated_decline` always
    # declines, so the payment-failure path can be exercised end to end.
    payment_method: PaymentMethod = "card_simulated"


class RestaurantResponseRequest(RequestModel):
    decision: Literal["accept", "reject"]
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class CancelOrderRequest(RequestModel):
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class RefundRequest(RequestModel):
    # Omitted = everything paid (total minus anything already refunded).
    amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    reason: str = Field(min_length=1, max_length=500)


class OrderItemResponse(BaseModel):
    id: str
    menu_item_id: str
    item_name_snapshot: str
    quantity: int
    unit_price: MoneyAmount
    special_instructions: str | None

    model_config = {"from_attributes": True}


class OrderStatusHistoryResponse(BaseModel):
    from_status: str | None
    to_status: str
    actor_id: str | None
    actor_role: str | None
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: str
    order_number: int
    customer_id: str
    restaurant_id: str
    status: str
    status_reason: str | None
    delivery_address_text: str
    delivery_lat: float
    delivery_lng: float
    special_instructions: str | None
    subtotal: MoneyAmount
    tax_amount: MoneyAmount
    delivery_fee: MoneyAmount
    discount_amount: MoneyAmount
    promo_code: str | None
    total_amount: MoneyAmount
    payment_method: str
    refunded_amount: MoneyAmount
    created_at: datetime
    confirmed_at: datetime | None
    assigned_at: datetime | None
    picked_up_at: datetime | None
    delivered_at: datetime | None
    rejected_at: datetime | None
    cancelled_at: datetime | None
    refunded_at: datetime | None
    items: list[OrderItemResponse] = []
    status_history: list[OrderStatusHistoryResponse] = []

    model_config = {"from_attributes": True}
