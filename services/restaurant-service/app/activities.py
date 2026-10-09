"""restaurant-service's activities on `restaurant-activities`
(docs/week2-plan.md step 2, D5): stock, capacity and promotions for the
order saga.

Concurrency: every check-and-change is a single conditional UPDATE
(`... WHERE stock_count >= :qty`), so two orders can never both take the
last unit — no read-then-write window. Within one transaction rows are
locked in a fixed order (restaurant row, then items by id), so concurrent
reservations queue behind each other instead of deadlocking.

Idempotency: each order has at most one `stock_reservations` and one
`promotion_redemptions` row (primary key = order_id). A retried activity
finds the row and doesn't apply the change twice. A release that arrives
before anything was reserved — the saga compensating after a reserve
attempt timed out — leaves a `released` tombstone, so a late-arriving
reserve becomes a no-op instead of taking stock nobody will give back."""

from collections import Counter

from sfo_contracts import BusinessErrorType, business_error
from sfo_contracts.activities import (
    OrderRef,
    RedeemPromotionInput,
    RedeemPromotionResult,
    ReserveOrderInput,
    ReserveOrderResult,
    RestaurantActivity,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from app import cache
from app.db import SessionFactory
from app.models import (
    MenuItem,
    Promotion,
    PromotionRedemption,
    RedemptionStatus,
    ReservationStatus,
    Restaurant,
    StockReservation,
)


async def _locked_reservation(session: AsyncSession, order_id: str) -> StockReservation | None:
    result = await session.execute(
        select(StockReservation).where(StockReservation.order_id == order_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def _free_capacity(session: AsyncSession, restaurant_id: str) -> None:
    await session.execute(
        update(Restaurant)
        .where(Restaurant.id == restaurant_id)
        .values(active_order_count=func.greatest(Restaurant.active_order_count - 1, 0))
    )


async def _restaurant_unavailable_reason(session: AsyncSession, restaurant_id: str) -> str:
    restaurant = await session.get(Restaurant, restaurant_id)
    if restaurant is None or restaurant.status != "active":
        return "not active"
    if not restaurant.is_accepting_orders:
        return "not accepting orders"
    return ""


@activity.defn(name=RestaurantActivity.RESERVE_ORDER)
async def reserve_order(data: ReserveOrderInput) -> ReserveOrderResult:
    """Takes one capacity slot and the ordered stock, atomically: all of it or
    none. Raises RestaurantUnavailable / CapacityFull / OutOfStock (not
    retried). An existing reservation for the order is returned as-is."""
    quantities = Counter[str]()
    for line in data.items:
        quantities[line.menu_item_id] += line.quantity

    async with SessionFactory() as session:
        # Claim the order first: a concurrent duplicate fails here, before
        # touching stock.
        session.add(
            StockReservation(
                order_id=data.order_id,
                restaurant_id=data.restaurant_id,
                items=[{"menu_item_id": i, "quantity": q} for i, q in sorted(quantities.items())],
                status=ReservationStatus.RESERVED,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            existing = await session.get(StockReservation, data.order_id)
            assert existing is not None
            return ReserveOrderResult(order_id=data.order_id, status=existing.status)

        taken = await session.execute(
            update(Restaurant)
            .where(
                Restaurant.id == data.restaurant_id,
                Restaurant.status == "active",
                Restaurant.is_accepting_orders.is_(True),
                Restaurant.active_order_count < Restaurant.max_concurrent_orders,
            )
            .values(active_order_count=Restaurant.active_order_count + 1)
            .returning(Restaurant.id)
        )
        if taken.first() is None:
            reason = await _restaurant_unavailable_reason(session, data.restaurant_id)
            if reason:
                raise business_error(
                    BusinessErrorType.RESTAURANT_UNAVAILABLE, f"Restaurant is {reason}"
                )
            raise business_error(
                BusinessErrorType.CAPACITY_FULL, "Restaurant is at its concurrent-order limit"
            )

        for item_id, quantity in sorted(quantities.items()):
            taken = await session.execute(
                update(MenuItem)
                .where(
                    MenuItem.id == item_id,
                    MenuItem.restaurant_id == data.restaurant_id,
                    MenuItem.is_available.is_(True),
                    (MenuItem.stock_count.is_(None)) | (MenuItem.stock_count >= quantity),
                )
                # NULL (unlimited) stays NULL: NULL - n is NULL.
                .values(stock_count=MenuItem.stock_count - quantity, version=MenuItem.version + 1)
                .returning(MenuItem.id)
            )
            if taken.first() is None:
                raise business_error(
                    BusinessErrorType.OUT_OF_STOCK,
                    f"Menu item {item_id} is unavailable or has fewer than {quantity} left",
                )

        await session.commit()
    await cache.invalidate(data.restaurant_id)
    return ReserveOrderResult(order_id=data.order_id, status=ReservationStatus.RESERVED)


@activity.defn(name=RestaurantActivity.RELEASE_RESERVATION)
async def release_reservation(data: OrderRef) -> None:
    """Compensation for reserve_order: gives the stock back and frees the
    capacity slot, if the reservation is still `reserved`; otherwise a no-op."""
    async with SessionFactory() as session:
        reservation = await _locked_reservation(session, data.order_id)
        if reservation is None:
            session.add(
                StockReservation(
                    order_id=data.order_id,
                    restaurant_id=None,
                    items=[],
                    status=ReservationStatus.RELEASED,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # reserve_order committed meanwhile — release that instead.
                await session.rollback()
                return await release_reservation(data)
            return
        if reservation.status != ReservationStatus.RESERVED:
            return
        restaurant_id = reservation.restaurant_id
        assert restaurant_id is not None
        await _free_capacity(session, restaurant_id)
        for line in sorted(reservation.items, key=lambda line: line["menu_item_id"]):
            await session.execute(
                update(MenuItem)
                .where(MenuItem.id == line["menu_item_id"], MenuItem.stock_count.is_not(None))
                .values(
                    stock_count=MenuItem.stock_count + line["quantity"],
                    version=MenuItem.version + 1,
                )
            )
        reservation.status = ReservationStatus.RELEASED
        await session.commit()
    await cache.invalidate(restaurant_id)


@activity.defn(name=RestaurantActivity.COMPLETE_RESERVATION)
async def complete_reservation(data: OrderRef) -> None:
    """At pickup: the order no longer occupies the kitchen, so its capacity
    slot is freed; the stock stays used. No-op unless still `reserved`."""
    async with SessionFactory() as session:
        reservation = await _locked_reservation(session, data.order_id)
        if reservation is None:
            raise business_error(
                BusinessErrorType.NOT_FOUND, f"No reservation for order {data.order_id}"
            )
        if reservation.status != ReservationStatus.RESERVED:
            return
        assert reservation.restaurant_id is not None
        await _free_capacity(session, reservation.restaurant_id)
        reservation.status = ReservationStatus.CONSUMED
        await session.commit()


@activity.defn(name=RestaurantActivity.REDEEM_PROMOTION)
async def redeem_promotion(data: RedeemPromotionInput) -> RedeemPromotionResult:
    """Uses one redemption of the restaurant's promo code: active, within its
    validity window, and under `max_uses` — checked and counted in one
    UPDATE. Raises PromotionInvalid otherwise (not retried)."""
    async with SessionFactory() as session:
        existing = await session.get(PromotionRedemption, data.order_id)
        if existing is not None:
            return await _existing_redemption(session, existing)

        result = await session.execute(
            update(Promotion)
            .where(
                Promotion.restaurant_id == data.restaurant_id,
                Promotion.code == data.code,
                Promotion.is_active.is_(True),
                Promotion.valid_from <= func.now(),
                Promotion.valid_until > func.now(),
                (Promotion.max_uses.is_(None)) | (Promotion.times_used < Promotion.max_uses),
            )
            .values(times_used=Promotion.times_used + 1)
            .returning(Promotion.id, Promotion.discount_type, Promotion.discount_value)
        )
        row = result.first()
        if row is None:
            raise business_error(
                BusinessErrorType.PROMOTION_INVALID,
                f"Promo code {data.code!r} is invalid, expired or used up",
            )
        session.add(
            PromotionRedemption(
                order_id=data.order_id, promotion_id=row.id, status=RedemptionStatus.REDEEMED
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            # A concurrent duplicate committed first; this attempt's
            # times_used increment is rolled back with it.
            await session.rollback()
            existing = await session.get(PromotionRedemption, data.order_id)
            assert existing is not None
            return await _existing_redemption(session, existing)
        return RedeemPromotionResult(
            promotion_id=row.id, discount_type=row.discount_type, discount_value=row.discount_value
        )


async def _existing_redemption(
    session: AsyncSession, redemption: PromotionRedemption
) -> RedeemPromotionResult:
    if redemption.status != RedemptionStatus.REDEEMED or redemption.promotion_id is None:
        raise business_error(
            BusinessErrorType.PROMOTION_INVALID,
            f"Promotion for order {redemption.order_id} was already released",
        )
    promotion = await session.get(Promotion, redemption.promotion_id)
    assert promotion is not None
    return RedeemPromotionResult(
        promotion_id=promotion.id,
        discount_type=promotion.discount_type,  # type: ignore[arg-type]
        discount_value=promotion.discount_value,
    )


@activity.defn(name=RestaurantActivity.RELEASE_PROMOTION)
async def release_promotion(data: OrderRef) -> None:
    """Compensation for redeem_promotion: gives the use back. No-op unless
    still `redeemed`; leaves a tombstone if nothing was redeemed yet."""
    async with SessionFactory() as session:
        result = await session.execute(
            select(PromotionRedemption)
            .where(PromotionRedemption.order_id == data.order_id)
            .with_for_update()
        )
        redemption = result.scalar_one_or_none()
        if redemption is None:
            session.add(
                PromotionRedemption(
                    order_id=data.order_id, promotion_id=None, status=RedemptionStatus.RELEASED
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return await release_promotion(data)
            return
        if redemption.status != RedemptionStatus.REDEEMED:
            return
        await session.execute(
            update(Promotion)
            .where(Promotion.id == redemption.promotion_id)
            .values(times_used=func.greatest(Promotion.times_used - 1, 0))
        )
        redemption.status = RedemptionStatus.RELEASED
        await session.commit()


ACTIVITIES = [
    reserve_order,
    release_reservation,
    complete_reservation,
    redeem_promotion,
    release_promotion,
]
