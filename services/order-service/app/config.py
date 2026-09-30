from sfo_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    # Source of truth for menu prices/availability at order-creation time
    # (app/restaurant_client.py).
    restaurant_service_url: str = "http://restaurant-service:8000"


settings = Settings()  # type: ignore[call-arg]
