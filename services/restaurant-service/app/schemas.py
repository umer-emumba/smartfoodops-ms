from datetime import datetime
from typing import Self

from pydantic import BaseModel, model_validator


class RestaurantCreate(BaseModel):
    name: str
    address: str
    city: str
    lat: float
    lng: float
    contact_phone: str
    cuisine_type: str | None = None


class RestaurantUpdate(BaseModel):
    # No `status` field here, deliberately — it must only change via the
    # system_admin-only /approve and /suspend actions
    # (docs/architecture.md#restaurant--rider-approval). Including it here
    # would let a restaurant_admin PATCH their own way to "active".
    name: str | None = None
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    contact_phone: str | None = None
    cuisine_type: str | None = None
    is_accepting_orders: bool | None = None
    max_concurrent_orders: int | None = None
    avg_prep_time_minutes: int | None = None


class RestaurantResponse(BaseModel):
    id: str
    admin_user_id: str
    name: str
    address: str
    city: str
    lat: float
    lng: float
    contact_phone: str
    cuisine_type: str | None
    status: str
    is_accepting_orders: bool
    max_concurrent_orders: int
    avg_prep_time_minutes: int
    created_at: datetime

    model_config = {"from_attributes": True}


class MenuCategoryCreate(BaseModel):
    name: str


class MenuCategoryUpdate(BaseModel):
    name: str | None = None


class MenuCategoryResponse(BaseModel):
    id: str
    restaurant_id: str
    name: str

    model_config = {"from_attributes": True}


class MenuItemCreate(BaseModel):
    category_id: str
    name: str
    description: str = ""
    price: float
    stock_count: int | None = None


class MenuItemUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    price: float | None = None
    is_available: bool | None = None
    stock_count: int | None = None


class MenuItemResponse(BaseModel):
    id: str
    restaurant_id: str
    category_id: str
    name: str
    description: str
    price: float
    is_available: bool
    stock_count: int | None
    version: int

    model_config = {"from_attributes": True}


class PromotionCreate(BaseModel):
    code: str
    discount_type: str  # percentage | fixed_amount
    discount_value: float
    valid_from: datetime
    valid_until: datetime
    max_uses: int | None = None

    @model_validator(mode="after")
    def _check_date_range(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self


class PromotionUpdate(BaseModel):
    # No `code` field, deliberately — renaming a promo code out from under
    # anyone already holding/redeeming it is more confusing than useful;
    # create a new code instead.
    discount_type: str | None = None
    discount_value: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    max_uses: int | None = None
    is_active: bool | None = None


class PromotionResponse(BaseModel):
    id: str
    restaurant_id: str
    code: str
    discount_type: str
    discount_value: float
    valid_from: datetime
    valid_until: datetime
    max_uses: int | None
    times_used: int
    is_active: bool

    model_config = {"from_attributes": True}
