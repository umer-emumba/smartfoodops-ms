from decimal import Decimal

import httpx
import pytest
from app import restaurant_client
from app.api.routes import _build_items
from app.restaurant_client import MenuItemSnapshot, RestaurantSnapshot
from app.schemas import MAX_ORDER_LINES, OrderCreate, OrderItemResponse
from pydantic import ValidationError
from sfo_common.errors import ConflictError, NotFoundError, ValidationAppError
from sfo_common.middleware import _correlation_id

RESTAURANT_ID = "r-1"


def _order(*items: tuple[str, int]) -> OrderCreate:
    return OrderCreate(
        restaurant_id=RESTAURANT_ID,
        delivery_address_text="1 Test St",
        delivery_lat=24.86,
        delivery_lng=67.01,
        items=[{"menu_item_id": i, "quantity": q} for i, q in items],
    )


def _restaurant(status: str, accepting: bool) -> RestaurantSnapshot:
    return RestaurantSnapshot(RESTAURANT_ID, status, accepting, "u-owner", 33.68, 73.04)


@pytest.fixture
def stub_restaurant(monkeypatch):
    state = {
        "restaurant": _restaurant("active", True),
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


async def test_prices_and_names_come_from_restaurant_service(stub_restaurant) -> None:
    restaurant, items = await _build_items(_order(("biryani", 2), ("wrap", 3)))
    assert restaurant.admin_user_id == "u-owner"
    assert [(i.item_name_snapshot, i.unit_price) for i in items] == [
        ("Chicken Biryani", Decimal("8.50")),
        ("Veggie Wrap", Decimal("0.10")),
    ]
    # Decimal, not float: 3 * 0.10 is exactly 0.30.
    assert sum(i.unit_price * i.quantity for i in items) == Decimal("17.30")


def test_client_cannot_send_price() -> None:
    order = _order(("biryani", 1))
    assert not hasattr(order.items[0], "unit_price")


async def test_unknown_item_rejected(stub_restaurant) -> None:
    with pytest.raises(ValidationAppError):
        await _build_items(_order(("not-on-menu", 1)))


async def test_unavailable_item_rejected(stub_restaurant) -> None:
    with pytest.raises(ConflictError):
        await _build_items(_order(("soldout", 1)))


async def test_stock_checked_across_split_lines(stub_restaurant) -> None:
    # 3 + 3 exceeds stock of 5 even though each line alone fits.
    with pytest.raises(ConflictError):
        await _build_items(_order(("biryani", 3), ("biryani", 3)))


async def test_quote_requests_each_item_once(stub_restaurant) -> None:
    await _build_items(_order(("biryani", 1), ("wrap", 1), ("biryani", 2)))
    assert stub_restaurant["requested"] == ["biryani", "wrap"]


async def test_restaurant_not_accepting_orders(stub_restaurant) -> None:
    stub_restaurant["restaurant"] = _restaurant("active", False)
    with pytest.raises(ConflictError):
        await _build_items(_order(("biryani", 1)))


async def test_inactive_restaurant_rejected(stub_restaurant) -> None:
    stub_restaurant["restaurant"] = _restaurant("suspended", True)
    with pytest.raises(NotFoundError):
        await _build_items(_order(("biryani", 1)))


def test_money_is_decimal_in_python_and_a_number_in_json() -> None:
    item = OrderItemResponse(
        id="i",
        menu_item_id="m",
        item_name_snapshot="Wrap",
        quantity=3,
        unit_price=Decimal("0.10"),
        special_instructions=None,
    )
    assert item.unit_price == Decimal("0.10")
    # The API contract stays a JSON number, and 0.10 doesn't turn into 0.1000000001.
    assert item.model_dump_json().count('"unit_price":0.1,') == 1


def test_order_line_count_and_text_bounded() -> None:
    with pytest.raises(ValidationError):
        _order(*[(f"item-{i}", 1) for i in range(MAX_ORDER_LINES + 1)])
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(
            {**_order(("biryani", 1)).model_dump(), "special_instructions": "x" * 501}
        )


def test_order_rejects_unknown_fields_including_client_prices() -> None:
    body = _order(("biryani", 1)).model_dump()
    with pytest.raises(ValidationError):
        OrderCreate.model_validate({**body, "total_amount": "0.01"})
    body["items"][0]["unit_price"] = "0.01"
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(body)


async def test_restaurant_call_forwards_trace_id() -> None:
    token = _correlation_id.set("trace-from-gateway")
    try:
        request = httpx.Request("POST", "http://restaurant-service/internal/x")
        for hook in restaurant_client.client.event_hooks["request"]:
            await hook(request)
    finally:
        _correlation_id.reset(token)
    assert request.headers["x-request-id"] == "trace-from-gateway"
