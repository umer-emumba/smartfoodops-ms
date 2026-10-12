from decimal import Decimal

import pytest
from app import pricing
from sfo_contracts.activities import GeoPoint, RedeemPromotionResult
from sfo_contracts.workflows import PricingConfig

CONFIG = PricingConfig(
    tax_rate=Decimal("0.05"), base_delivery_fee=Decimal("100"), per_km_fee=Decimal("30")
)


def _promo(kind: str, value: str) -> RedeemPromotionResult:
    return RedeemPromotionResult(
        promotion_id="p",
        discount_type=kind,
        discount_value=Decimal(value),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "promotion,expected",
    [
        (None, "0.00"),
        (_promo("percentage", "10"), "100.00"),
        (_promo("percentage", "12.5"), "125.00"),
        (_promo("fixed_amount", "150"), "150.00"),
        (_promo("fixed_amount", "5000"), "1000.00"),  # never more than the subtotal
        (_promo("percentage", "150"), "1000.00"),
    ],
)
def test_discount(promotion: RedeemPromotionResult | None, expected: str) -> None:
    assert pricing.discount(Decimal("1000"), promotion) == Decimal(expected)


def test_tax_is_on_the_discounted_subtotal_rounded_to_cents() -> None:
    assert pricing.tax(Decimal("1000"), Decimal("100"), CONFIG) == Decimal("45.00")
    assert pricing.tax(Decimal("10.10"), Decimal("0"), CONFIG) == Decimal("0.51")  # 0.505 → up


def test_delivery_fee_by_straight_line_distance() -> None:
    # 0.02° of latitude ≈ 2.22 km → 2.2 km → 100 + 30 × 2.2.
    pickup = GeoPoint(lat=33.6844, lng=73.0479)
    dropoff = GeoPoint(lat=33.7044, lng=73.0479)
    assert pricing.delivery_fee(pickup, dropoff, CONFIG) == Decimal("166.00")
    assert pricing.delivery_fee(pickup, pickup, CONFIG) == Decimal("100.00")


def test_delivery_fee_without_pickup_location_is_the_base_fee() -> None:
    assert pricing.delivery_fee(None, GeoPoint(lat=0, lng=0), CONFIG) == Decimal("100.00")
