from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """tracking-service has no database (docs/week2-plan.md D8), so it doesn't
    inherit sfo_common.BaseServiceSettings, which requires DATABASE_URL."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "tracking-service"
    environment: str = "local"
    log_level: str = "INFO"

    redis_url: str = "redis://redis:6379/0"

    jwt_secret: str
    jwt_algorithm: str = "HS256"

    # Only used when a Redis state key is missing (app/state.py).
    order_service_url: str = "http://order-service:8000"
    restaurant_service_url: str = "http://restaurant-service:8000"

    # A comment line on an idle stream: keeps proxies from closing it and
    # lets both ends notice a dead connection.
    heartbeat_seconds: float = 15
    # Messages buffered per client; a client this far behind is disconnected
    # (it reconnects and gets a fresh snapshot).
    client_queue_size: int = 100


settings = Settings()  # type: ignore[call-arg]
