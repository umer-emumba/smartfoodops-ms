from decimal import Decimal

import pytest
from app import restaurant_client
from app.api.routes import _build_items
from app.restaurant_client import MenuItemSnapshot, RestaurantSnapshot
from app.schemas import OrderCreate
from sfo_common.errors import ConflictError, NotFoundError, ValidationAppError

RESTAURANT_ID = "r-1"


def _order(*items: tuple[str, int]) -> OrderCreate:
    return OrderCreate(
        restaurant_id=RESTAURANT_ID,
        delivery_address_text="1 Test St",
        delivery_lat=24.86,
        delivery_lng=67.01,
        items=[{"menu_item_id": i, "quantity": q} for i, q in items],
    )


@pytest.fixture
def stub_restaurant(monkeypatch):
    state = {
        "restaurant": RestaurantSnapshot(RESTAURANT_ID, "active", True),
        "menu": {
            "biryani": MenuItemSnapshot("biryani", "Chicken Biryani", Decimal("8.50"), True, 5),
            "wrap": MenuItemSnapshot("wrap", "Veggie Wrap", Decimal("0.10"), True, None),
            "soldout": MenuItemSnapshot("soldout", "Sold Out", Decimal("3.00"), False, None),
        },
    }

    async def get_quote(_: str, item_ids: list[str]):
        # Like the real endpoint: only requested items that are on the menu.
        state["requested"] = item_ids
        menu = state["menu"]
        return state["restaurant"], {i: menu[i] for i in item_ids if i in menu}

    monkeypatch.setattr(restaurant_client, "get_quote", get_quote)
    return state


@pytest.mark.asyncio
async def test_prices_and_names_come_from_restaurant_service(stub_restaurant) -> None:
    items = await _build_items(_order(("biryani", 2), ("wrap", 3)))
    assert [(i.item_name_snapshot, i.unit_price) for i in items] == [
        ("Chicken Biryani", Decimal("8.50")),
        ("Veggie Wrap", Decimal("0.10")),
    ]
    # Decimal, not float: 3 * 0.10 is exactly 0.30.
    assert sum(i.unit_price * i.quantity for i in items) == Decimal("17.30")


def test_client_cannot_send_price() -> None:
    order = _order(("biryani", 1))
    assert not hasattr(order.items[0], "unit_price")


@pytest.mark.asyncio
async def test_unknown_item_rejected(stub_restaurant) -> None:
    with pytest.raises(ValidationAppError):
        await _build_items(_order(("not-on-menu", 1)))


@pytest.mark.asyncio
async def test_unavailable_item_rejected(stub_restaurant) -> None:
    with pytest.raises(ConflictError):
        await _build_items(_order(("soldout", 1)))


@pytest.mark.asyncio
async def test_stock_checked_across_split_lines(stub_restaurant) -> None:
    # 3 + 3 exceeds stock of 5 even though each line alone fits.
    with pytest.raises(ConflictError):
        await _build_items(_order(("biryani", 3), ("biryani", 3)))


@pytest.mark.asyncio
async def test_quote_requests_each_item_once(stub_restaurant) -> None:
    await _build_items(_order(("biryani", 1), ("wrap", 1), ("biryani", 2)))
    assert stub_restaurant["requested"] == ["biryani", "wrap"]


@pytest.mark.asyncio
async def test_restaurant_not_accepting_orders(stub_restaurant) -> None:
    stub_restaurant["restaurant"] = RestaurantSnapshot(RESTAURANT_ID, "active", False)
    with pytest.raises(ConflictError):
        await _build_items(_order(("biryani", 1)))


@pytest.mark.asyncio
async def test_inactive_restaurant_rejected(stub_restaurant) -> None:
    stub_restaurant["restaurant"] = RestaurantSnapshot(RESTAURANT_ID, "suspended", True)
    with pytest.raises(NotFoundError):
        await _build_items(_order(("biryani", 1)))
