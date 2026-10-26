from decimal import Decimal

from sfo_common import BaseServiceSettings
from sfo_common.config import OutboxRelaySettings, TemporalSettings


class Settings(BaseServiceSettings, TemporalSettings, OutboxRelaySettings):
    # Source of truth for menu prices/availability at order-creation time
    # (app/restaurant_client.py).
    restaurant_service_url: str = "http://restaurant-service:8000"

    # Order saga (docs/week2-plan.md §7). Pricing values are placeholders to
    # confirm with the mentor; each order captures them when its saga starts.
    tax_rate: Decimal = Decimal("0.05")  # of (subtotal - discount)
    base_delivery_fee: Decimal = Decimal("100")  # PKR
    per_km_fee: Decimal = Decimal("30")  # PKR per straight-line km
    restaurant_accept_timeout_seconds: int = 300
    # Rider assignment: 60s per offer; a rider who declined or let it expire
    # is asked again after 2 minutes. While nobody is free, look again every
    # 30s, one radius step farther out (5 → 8 → 12 km around the restaurant).
    # After 10 minutes the order is cancelled (`no_rider_available`).
    rider_offer_timeout_seconds: int = 60
    rider_reoffer_after_seconds: int = 120
    rider_search_timeout_seconds: int = 600
    rider_search_retry_seconds: int = 30
    rider_search_radii_km: list[float] = [5.0, 8.0, 12.0]
    # Kitchen: food not marked ready 15 minutes after it was due (confirmation
    # + prep time) is flagged; after an hour the order is cancelled
    # (`kitchen_timeout`). Rider: ready food not picked up 15 minutes after
    # the later of "ready" and their pickup ETA → another rider. Delivery:
    # not delivered 2 hours after pickup → flagged for support.
    food_overdue_seconds: int = 900
    kitchen_timeout_seconds: int = 3600
    pickup_grace_seconds: int = 900
    delivery_overdue_seconds: int = 7200
    # Orders whose drop-off is farther than this (straight line) from the
    # restaurant are refused at creation — beyond it there's no realistic
    # delivery (rider search covers up to 12 km around the restaurant).
    max_delivery_km: float = 15

    # Reconciler (app/reconciler.py): restarts sagas for orders stuck in
    # `created` — committed but whose workflow never started.
    reconcile_interval_seconds: float = 30
    reconcile_min_age_seconds: int = 60
    # Older stranded orders are only logged, not restarted: by then the
    # customer has likely given up, and starting a saga hours late would
    # reserve stock and charge them out of the blue.
    reconcile_max_age_seconds: int = 3600


settings = Settings()  # type: ignore[call-arg]
