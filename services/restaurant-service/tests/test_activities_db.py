"""restaurant-activities against a real Postgres (week2-plan step 2). The
concurrency tests are the point: they show the conditional UPDATEs prevent
overselling stock, capacity and promo uses, which mocks can't."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app import activities
from app.models import (
    MenuCategory,
    MenuItem,
    Promotion,
    PromotionRedemption,
    Restaurant,
    StockReservation,
)
from sfo_contracts import BusinessErrorType
from sfo_contracts.activities import (
    OrderRef,
    RedeemPromotionInput,
    ReservedItem,
    ReserveOrderInput,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import ApplicationError

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]
RESTAURANT = "r-1"


async def _setup(
    factory: Factory,
    *,
    stock: dict[str, int | None],
    max_concurrent_orders: int = 100,
    status: str = "active",
    accepting: bool = True,
) -> None:
    async with factory() as session:
        session.add(
            Restaurant(
                id=RESTAURANT,
                admin_user_id="u-owner",
                name="Test Kitchen",
                address="1 Test St",
                city="Islamabad",
                lat=33.68,
                lng=73.04,
                contact_phone="+923000000000",
                status=status,
                is_accepting_orders=accepting,
                max_concurrent_orders=max_concurrent_orders,
            )
        )
        # No ORM relationships between these models, so the unit of work
        # doesn't order inserts by foreign key — flush parents first.
        await session.flush()
        session.add(MenuCategory(id="c-1", restaurant_id=RESTAURANT, name="Mains"))
        await session.flush()
        for item_id, count in stock.items():
            session.add(
                MenuItem(
                    id=item_id,
                    restaurant_id=RESTAURANT,
                    category_id="c-1",
                    name=item_id,
                    price=Decimal("500"),
                    stock_count=count,
                )
            )
        await session.commit()


async def _stock(factory: Factory, item_id: str) -> int | None:
    async with factory() as session:
        item = await session.get(MenuItem, item_id)
        assert item is not None
        return item.stock_count


async def _active_orders(factory: Factory) -> int:
    async with factory() as session:
        restaurant = await session.get(Restaurant, RESTAURANT)
        assert restaurant is not None
        return restaurant.active_order_count


def _reserve(order_id: str, *items: tuple[str, int]) -> ReserveOrderInput:
    return ReserveOrderInput(
        order_id=order_id,
        restaurant_id=RESTAURANT,
        items=[ReservedItem(menu_item_id=i, quantity=q) for i, q in items],
    )


async def _attempt_all(calls: list) -> tuple[int, list[str]]:
    """Runs the calls concurrently; returns (successes, business error types)."""
    results = await asyncio.gather(*calls, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    unexpected = [e for e in errors if not isinstance(e, ApplicationError)]
    assert not unexpected, unexpected
    return len(results) - len(errors), [e.type for e in errors]  # type: ignore[union-attr]


# ---------- reserve / release / complete ----------


async def test_reserve_takes_stock_and_capacity(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"biryani": 10, "naan": None})
    result = await activities.reserve_order(
        _reserve("o-1", ("biryani", 2), ("naan", 3), ("biryani", 1))
    )
    assert result.status == "reserved"
    assert await _stock(session_factory, "biryani") == 7  # split lines summed
    assert await _stock(session_factory, "naan") is None  # unlimited stays unlimited
    assert await _active_orders(session_factory) == 1


async def test_out_of_stock_takes_nothing(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"biryani": 10, "kebab": 1})
    with pytest.raises(ApplicationError) as exc_info:
        await activities.reserve_order(_reserve("o-1", ("biryani", 2), ("kebab", 2)))
    assert exc_info.value.type == BusinessErrorType.OUT_OF_STOCK
    assert exc_info.value.non_retryable
    # All or nothing: biryani's decrement and the capacity slot rolled back.
    assert await _stock(session_factory, "biryani") == 10
    assert await _active_orders(session_factory) == 0
    async with session_factory() as session:
        assert await session.get(StockReservation, "o-1") is None


@pytest.mark.parametrize(
    "setup,error",
    [
        ({"status": "suspended"}, BusinessErrorType.RESTAURANT_UNAVAILABLE),
        ({"accepting": False}, BusinessErrorType.RESTAURANT_UNAVAILABLE),
        ({"max_concurrent_orders": 0}, BusinessErrorType.CAPACITY_FULL),
    ],
)
async def test_restaurant_must_be_able_to_take_the_order(
    session_factory: Factory, setup: dict, error: BusinessErrorType
) -> None:
    await _setup(session_factory, stock={"biryani": 10}, **setup)
    with pytest.raises(ApplicationError) as exc_info:
        await activities.reserve_order(_reserve("o-1", ("biryani", 1)))
    assert exc_info.value.type == error
    assert await _stock(session_factory, "biryani") == 10


async def test_item_from_another_restaurant_is_out_of_stock(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"biryani": 10})
    with pytest.raises(ApplicationError) as exc_info:
        await activities.reserve_order(_reserve("o-1", ("not-on-this-menu", 1)))
    assert exc_info.value.type == BusinessErrorType.OUT_OF_STOCK


async def test_reserve_and_release_are_idempotent(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"biryani": 10})
    await activities.reserve_order(_reserve("o-1", ("biryani", 3)))
    await activities.reserve_order(_reserve("o-1", ("biryani", 3)))  # retry
    assert await _stock(session_factory, "biryani") == 7
    assert await _active_orders(session_factory) == 1

    await activities.release_reservation(OrderRef("o-1"))
    await activities.release_reservation(OrderRef("o-1"))  # retry
    assert await _stock(session_factory, "biryani") == 10
    assert await _active_orders(session_factory) == 0


async def test_release_before_reserve_blocks_a_late_reserve(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"biryani": 10})
    await activities.release_reservation(OrderRef("o-1"))
    result = await activities.reserve_order(_reserve("o-1", ("biryani", 3)))
    assert result.status == "released"
    assert await _stock(session_factory, "biryani") == 10
    assert await _active_orders(session_factory) == 0


async def test_complete_frees_capacity_and_keeps_stock_used(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"biryani": 10})
    await activities.reserve_order(_reserve("o-1", ("biryani", 3)))
    await activities.complete_reservation(OrderRef("o-1"))
    await activities.complete_reservation(OrderRef("o-1"))  # retry
    await activities.release_reservation(OrderRef("o-1"))  # too late: no-op
    assert await _stock(session_factory, "biryani") == 7
    assert await _active_orders(session_factory) == 0


async def test_complete_without_reservation_is_an_error(session_factory: Factory) -> None:
    await _setup(session_factory, stock={})
    with pytest.raises(ApplicationError) as exc_info:
        await activities.complete_reservation(OrderRef("o-1"))
    assert exc_info.value.type == BusinessErrorType.NOT_FOUND


# ---------- concurrency ----------


async def test_concurrent_orders_never_oversell_stock(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"biryani": 5})
    successes, errors = await _attempt_all(
        [activities.reserve_order(_reserve(f"o-{n}", ("biryani", 1))) for n in range(20)]
    )
    assert successes == 5
    assert errors == [BusinessErrorType.OUT_OF_STOCK] * 15
    assert await _stock(session_factory, "biryani") == 0
    assert await _active_orders(session_factory) == 5


async def test_concurrent_orders_never_exceed_capacity(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"naan": None}, max_concurrent_orders=3)
    successes, errors = await _attempt_all(
        [activities.reserve_order(_reserve(f"o-{n}", ("naan", 1))) for n in range(10)]
    )
    assert successes == 3
    assert errors == [BusinessErrorType.CAPACITY_FULL] * 7
    assert await _active_orders(session_factory) == 3


async def test_concurrent_retries_of_one_order_reserve_once(session_factory: Factory) -> None:
    await _setup(session_factory, stock={"biryani": 10})
    successes, _ = await _attempt_all(
        [activities.reserve_order(_reserve("o-1", ("biryani", 2))) for _ in range(5)]
    )
    assert successes == 5  # every attempt reports the one reservation
    assert await _stock(session_factory, "biryani") == 8
    assert await _active_orders(session_factory) == 1


async def test_concurrent_orders_over_shared_items_do_not_deadlock(
    session_factory: Factory,
) -> None:
    # Opposite line order in the requests; rows are still locked in one order.
    await _setup(session_factory, stock={"a": 100, "b": 100})
    calls = [
        activities.reserve_order(
            _reserve(f"o-{n}", *([("a", 1), ("b", 1)] if n % 2 else [("b", 1), ("a", 1)]))
        )
        for n in range(20)
    ]
    successes, _ = await _attempt_all(calls)
    assert successes == 20
    assert (await _stock(session_factory, "a"), await _stock(session_factory, "b")) == (80, 80)


# ---------- promotions ----------


async def _promotion(
    factory: Factory,
    *,
    code: str = "WELCOME10",
    max_uses: int | None = None,
    times_used: int = 0,
    is_active: bool = True,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> str:
    now = datetime.now(UTC)
    promotion_id = str(uuid.uuid4())
    async with factory() as session:
        session.add(
            Promotion(
                id=promotion_id,
                restaurant_id=RESTAURANT,
                code=code,
                discount_type="percentage",
                discount_value=Decimal("10"),
                valid_from=valid_from or now - timedelta(days=1),
                valid_until=valid_until or now + timedelta(days=1),
                max_uses=max_uses,
                times_used=times_used,
                is_active=is_active,
            )
        )
        await session.commit()
    return promotion_id


async def _times_used(factory: Factory, promotion_id: str) -> int:
    async with factory() as session:
        promotion = await session.get(Promotion, promotion_id)
        assert promotion is not None
        return promotion.times_used


def _redeem(order_id: str, code: str = "WELCOME10") -> RedeemPromotionInput:
    return RedeemPromotionInput(order_id=order_id, restaurant_id=RESTAURANT, code=code)


async def test_redeem_and_release_once(session_factory: Factory) -> None:
    await _setup(session_factory, stock={})
    promotion_id = await _promotion(session_factory)
    first = await activities.redeem_promotion(_redeem("o-1"))
    again = await activities.redeem_promotion(_redeem("o-1"))  # retry
    assert first == again
    assert (first.discount_type, first.discount_value) == ("percentage", Decimal("10.00"))
    assert await _times_used(session_factory, promotion_id) == 1

    await activities.release_promotion(OrderRef("o-1"))
    await activities.release_promotion(OrderRef("o-1"))  # retry
    assert await _times_used(session_factory, promotion_id) == 0
    async with session_factory() as session:
        redemption = await session.get(PromotionRedemption, "o-1")
        assert redemption is not None and redemption.status == "released"


@pytest.mark.parametrize(
    "promotion",
    [
        {"is_active": False},
        {"valid_until": datetime.now(UTC) - timedelta(minutes=1)},
        {"valid_from": datetime.now(UTC) + timedelta(days=1)},
        {"max_uses": 3, "times_used": 3},
        {"code": "OTHERCODE"},
    ],
    ids=["inactive", "expired", "not-started", "used-up", "unknown-code"],
)
async def test_invalid_promotion_is_rejected(session_factory: Factory, promotion: dict) -> None:
    await _setup(session_factory, stock={})
    promotion_id = await _promotion(session_factory, **promotion)
    with pytest.raises(ApplicationError) as exc_info:
        await activities.redeem_promotion(_redeem("o-1"))
    assert exc_info.value.type == BusinessErrorType.PROMOTION_INVALID
    assert exc_info.value.non_retryable
    assert await _times_used(session_factory, promotion_id) == promotion.get("times_used", 0)


async def test_last_promo_use_goes_to_exactly_one_order(session_factory: Factory) -> None:
    await _setup(session_factory, stock={})
    promotion_id = await _promotion(session_factory, max_uses=1)
    successes, errors = await _attempt_all(
        [activities.redeem_promotion(_redeem(f"o-{n}")) for n in range(2)]
    )
    assert (successes, errors) == (1, [BusinessErrorType.PROMOTION_INVALID])
    assert await _times_used(session_factory, promotion_id) == 1


async def test_release_before_redeem_blocks_a_late_redeem(session_factory: Factory) -> None:
    await _setup(session_factory, stock={})
    promotion_id = await _promotion(session_factory)
    await activities.release_promotion(OrderRef("o-1"))
    with pytest.raises(ApplicationError) as exc_info:
        await activities.redeem_promotion(_redeem("o-1"))
    assert exc_info.value.type == BusinessErrorType.PROMOTION_INVALID
    assert await _times_used(session_factory, promotion_id) == 0
