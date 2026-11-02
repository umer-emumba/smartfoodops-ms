from sfo_common import BaseServiceSettings
from sfo_common.config import TemporalSettings


class Settings(BaseServiceSettings, TemporalSettings):
    # Review eligibility checks (app/order_client.py).
    order_service_url: str = "http://order-service:8000"


settings = Settings()  # type: ignore[call-arg]
