from sfo_common import BaseServiceSettings
from sfo_common.config import TemporalSettings


class Settings(BaseServiceSettings, TemporalSettings):
    # Source of truth for menu prices/availability at order-creation time
    # (app/restaurant_client.py).
    restaurant_service_url: str = "http://restaurant-service:8000"


settings = Settings()  # type: ignore[call-arg]
