from decimal import Decimal

from sfo_common import BaseServiceSettings
from sfo_common.config import TemporalSettings


class Settings(BaseServiceSettings, TemporalSettings):
    # Source of truth for menu prices/availability at order-creation time
    # (app/restaurant_client.py).
    restaurant_service_url: str = "http://restaurant-service:8000"

    # Order saga (docs/week2-plan.md §7). Pricing values are placeholders to
    # confirm with the mentor; each order captures them when its saga starts.
    tax_rate: Decimal = Decimal("0.05")  # of (subtotal - discount)
    base_delivery_fee: Decimal = Decimal("100")  # PKR
    per_km_fee: Decimal = Decimal("30")  # PKR per straight-line km
    restaurant_accept_timeout_seconds: int = 300
    # Orders whose drop-off is farther than this (straight line) from the
    # restaurant are refused at creation — beyond it there's no realistic
    # delivery (rider search covers 5 km around the restaurant, step 5).
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
