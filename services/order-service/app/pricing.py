"""Order pricing (docs/week2-plan.md step 4): pure, deterministic functions
— the saga calls them directly (no activity needed) and a replay computes
the same values.

    discount     = promotion: percentage of subtotal, or a fixed amount (≤ subtotal)
    tax          = tax_rate × (subtotal − discount)
    delivery fee = base fee + per-km fee × straight-line km, restaurant → customer
    total        = subtotal − discount + tax + delivery fee  (apply_pricing activity)
"""

from decimal import ROUND_HALF_UP, Decimal

from sfo_common.geo import haversine_km
from sfo_contracts.activities import GeoPoint, RedeemPromotionResult
from sfo_contracts.workflows import PricingConfig

_CENTS = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def discount(subtotal: Decimal, promotion: RedeemPromotionResult | None) -> Decimal:
    if promotion is None:
        return Decimal("0.00")
    if promotion.discount_type == "percentage":
        amount = subtotal * promotion.discount_value / Decimal(100)
    else:
        amount = promotion.discount_value
    return _money(min(amount, subtotal))


def tax(subtotal: Decimal, discount_amount: Decimal, config: PricingConfig) -> Decimal:
    return _money((subtotal - discount_amount) * config.tax_rate)


def delivery_fee(pickup: GeoPoint | None, dropoff: GeoPoint, config: PricingConfig) -> Decimal:
    if pickup is None:
        return _money(config.base_delivery_fee)
    km = haversine_km(pickup.lat, pickup.lng, dropoff.lat, dropoff.lng)
    # Distance rounded to 0.1 km before pricing, so the fee doesn't depend on
    # float noise in the last digits.
    km_tenths = Decimal(str(round(km, 1)))
    return _money(config.base_delivery_fee + config.per_km_fee * km_tenths)
