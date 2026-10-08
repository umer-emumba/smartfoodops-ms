from datetime import datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]
# Decimal, not float, for money: Numeric(10,2) in the DB, and a value that
# doesn't fit (too many digits/decimals) is a 422 here instead of a DB error.
Money = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]
DiscountType = Literal["percentage", "fixed_amount"]


class _PatchModel(BaseModel):
    """Base for PATCH bodies. `exclude_unset` still keeps an explicit
    `{"name": null}`, which would write NULL into a NOT NULL column (a 500).
    Rejects that as a 422 unless the field is genuinely nullable."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> Self:
        bad = sorted(
            f
            for f in self.model_fields_set
            if getattr(self, f) is None and f not in self.nullable_fields
        )
        if bad:
            raise ValueError(f"{', '.join(bad)} cannot be null")
        return self


class RestaurantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1)
    city: str = Field(min_length=1)
    lat: Latitude
    lng: Longitude
    contact_phone: str = Field(min_length=5, max_length=20)
    cuisine_type: str | None = None


class RestaurantUpdate(_PatchModel):
    # No `status` field here, deliberately — it must only change via the
    # system_admin-only /approve and /suspend actions
    # (docs/architecture.md#restaurant--rider-approval). Including it here
    # would let a restaurant_admin PATCH their own way to "active".
    nullable_fields = frozenset({"cuisine_type"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, min_length=1)
    lat: Latitude | None = None
    lng: Longitude | None = None
    contact_phone: str | None = Field(default=None, min_length=5, max_length=20)
    cuisine_type: str | None = None
    is_accepting_orders: bool | None = None
    max_concurrent_orders: int | None = Field(default=None, ge=1)
    avg_prep_time_minutes: int | None = Field(default=None, ge=1, le=240)


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
    name: str = Field(min_length=1, max_length=100)


class MenuCategoryUpdate(_PatchModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)


class MenuCategoryResponse(BaseModel):
    id: str
    restaurant_id: str
    name: str

    model_config = {"from_attributes": True}


class MenuItemCreate(BaseModel):
    category_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    price: Money
    stock_count: int | None = Field(default=None, ge=0)  # None = unlimited


class MenuItemUpdate(_PatchModel):
    # stock_count: explicit null is meaningful — it switches to unlimited stock.
    nullable_fields = frozenset({"stock_count"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    price: Money | None = None
    is_available: bool | None = None
    stock_count: int | None = Field(default=None, ge=0)


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


def check_promotion_rules(
    discount_type: str, discount_value: Decimal, valid_from: datetime, valid_until: datetime
) -> None:
    """Shared by PromotionCreate and the PATCH route (which re-checks after
    merging a partial update onto the stored row). Raises ValueError."""
    if valid_until <= valid_from:
        raise ValueError("valid_until must be after valid_from")
    if discount_type == "percentage" and discount_value > 100:
        raise ValueError("a percentage discount cannot exceed 100")


class PromotionCreate(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    discount_type: DiscountType
    discount_value: Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
    # AwareDatetime: a timezone is required. Comparing a naive datetime with
    # the timezone-aware values stored in Postgres raises TypeError (a 500).
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    max_uses: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        check_promotion_rules(
            self.discount_type, self.discount_value, self.valid_from, self.valid_until
        )
        return self


class PromotionUpdate(_PatchModel):
    # No `code` field, deliberately — renaming a promo code out from under
    # anyone already holding/redeeming it is more confusing than useful;
    # create a new code instead.
    nullable_fields = frozenset({"max_uses"})  # null = unlimited uses

    discount_type: DiscountType | None = None
    discount_value: Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)] | None = None
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    max_uses: int | None = Field(default=None, ge=1)
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


class StatusChangeRequest(BaseModel):
    """Optional body for approve/suspend — recorded in the audit trail."""

    reason: str | None = Field(default=None, max_length=500)


class StatusHistoryResponse(BaseModel):
    from_status: str | None
    to_status: str
    actor_id: str
    actor_role: str
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Internal: order-service's price/stock quote (app/api/internal.py) ---


class QuoteRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=200)


class QuoteRestaurant(BaseModel):
    id: str
    status: str
    is_accepting_orders: bool

    model_config = {"from_attributes": True}


class QuoteItem(BaseModel):
    id: str
    name: str
    # Decimal (serialized as a string), not float — order-service totals
    # money with Decimal and shouldn't inherit float rounding.
    price: Decimal
    is_available: bool
    stock_count: int | None

    model_config = {"from_attributes": True}


class QuoteResponse(BaseModel):
    restaurant: QuoteRestaurant
    items: list[QuoteItem]
