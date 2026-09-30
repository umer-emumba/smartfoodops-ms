from datetime import datetime

from pydantic import BaseModel, Field


class OrderItemCreate(BaseModel):
    menu_item_id: str
    # Client-supplied for now (Week 1 skeleton, no saga yet). Week 2's saga
    # validates stock/price against restaurant-service server-side instead of
    # trusting these — see docs/PROGRESS.md.
    item_name: str
    quantity: int = Field(gt=0)
    unit_price: float = Field(ge=0)
    special_instructions: str | None = None


class OrderCreate(BaseModel):
    restaurant_id: str
    delivery_address_text: str
    delivery_lat: float
    delivery_lng: float
    special_instructions: str | None = None
    promo_code: str | None = None
    items: list[OrderItemCreate] = Field(min_length=1)


class OrderItemResponse(BaseModel):
    id: str
    menu_item_id: str
    item_name_snapshot: str
    quantity: int
    unit_price: float
    special_instructions: str | None

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
    subtotal: float
    tax_amount: float
    delivery_fee: float
    discount_amount: float
    promo_code: str | None
    total_amount: float
    created_at: datetime
    confirmed_at: datetime | None
    rejected_at: datetime | None
    cancelled_at: datetime | None
    items: list[OrderItemResponse] = []

    model_config = {"from_attributes": True}
