from datetime import datetime

from pydantic import BaseModel, Field
from sfo_common.money import MoneyAmount


class OrderItemCreate(BaseModel):
    """Only *what* and *how many* — name and price are looked up server-side
    from restaurant-service (app/restaurant_client.py), never trusted from the
    client."""

    menu_item_id: str
    quantity: int = Field(gt=0, le=50)
    special_instructions: str | None = None


class OrderCreate(BaseModel):
    restaurant_id: str
    delivery_address_text: str
    delivery_lat: float = Field(ge=-90, le=90)
    delivery_lng: float = Field(ge=-180, le=180)
    special_instructions: str | None = None
    promo_code: str | None = None
    items: list[OrderItemCreate] = Field(min_length=1)


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
    created_at: datetime
    confirmed_at: datetime | None
    rejected_at: datetime | None
    cancelled_at: datetime | None
    items: list[OrderItemResponse] = []
    status_history: list[OrderStatusHistoryResponse] = []

    model_config = {"from_attributes": True}
