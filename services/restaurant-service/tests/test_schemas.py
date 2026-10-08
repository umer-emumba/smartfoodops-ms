from decimal import Decimal

import pytest
from app.schemas import (
    MenuItemCreate,
    MenuItemUpdate,
    PromotionCreate,
    RestaurantCreate,
    RestaurantUpdate,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sfo_common.errors import register_exception_handlers

_PROMO = {
    "code": "WELCOME10",
    "discount_type": "percentage",
    "discount_value": "10",
    "valid_from": "2026-10-01T00:00:00Z",
    "valid_until": "2026-11-01T00:00:00Z",
}


def test_explicit_null_on_required_field_rejected() -> None:
    with pytest.raises(ValidationError, match="name cannot be null"):
        RestaurantUpdate.model_validate({"name": None})


def test_explicit_null_allowed_where_meaningful() -> None:
    # null stock_count = switch to unlimited stock; null cuisine_type = clear it.
    assert MenuItemUpdate.model_validate({"stock_count": None}).model_fields_set == {"stock_count"}
    RestaurantUpdate.model_validate({"cuisine_type": None})


def test_price_is_decimal_and_non_negative() -> None:
    item = MenuItemCreate.model_validate({"category_id": "c", "name": "Tea", "price": 0.1})
    assert item.price == Decimal("0.1")
    with pytest.raises(ValidationError):
        MenuItemCreate.model_validate({"category_id": "c", "name": "Tea", "price": -1})


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        PromotionCreate.model_validate({**_PROMO, "valid_from": "2026-10-01T00:00:00"})


def test_percentage_capped_and_type_validated() -> None:
    PromotionCreate.model_validate(_PROMO)
    with pytest.raises(ValidationError, match="cannot exceed 100"):
        PromotionCreate.model_validate({**_PROMO, "discount_value": "150"})
    with pytest.raises(ValidationError):
        PromotionCreate.model_validate({**_PROMO, "discount_type": "bogus"})


def test_bad_coordinates_rejected() -> None:
    with pytest.raises(ValidationError):
        RestaurantUpdate.model_validate({"lat": 200})


async def test_custom_validator_error_is_a_422_envelope() -> None:
    """Regression: ValueError from a model validator used to crash the shared
    422 handler (non-serializable ctx) and surface as a 500."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.patch("/r")
    async def patch(body: RestaurantUpdate) -> dict:
        return {}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.patch("/r", json={"name": None})
    assert r.status_code == 422
    assert "cannot be null" in r.text


def test_restaurant_create_hardened() -> None:
    base = {
        "name": "Kitchen",
        "address": "1 St",
        "city": "Karachi",
        "lat": 1,
        "lng": 1,
        "contact_phone": "+92210000000",
    }
    RestaurantCreate.model_validate(base)
    for bad in [
        {**base, "status": "active"},  # unknown field: no self-approval via create
        {**base, "contact_phone": "021-1234"},
        {**base, "address": "x" * 501},
    ]:
        with pytest.raises(ValidationError):
            RestaurantCreate.model_validate(bad)
