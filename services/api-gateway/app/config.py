from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """api-gateway has no database of its own — see docs/architecture.md's
    services table — so it does not inherit sfo_common.BaseServiceSettings,
    which requires DATABASE_URL."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "api-gateway"
    environment: str = "local"
    log_level: str = "INFO"

    redis_url: str = "redis://redis:6379/0"

    jwt_secret: str
    jwt_algorithm: str = "HS256"

    cors_origins: str = ""

    user_service_url: str = "http://user-service:8000"
    restaurant_service_url: str = "http://restaurant-service:8000"
    order_service_url: str = "http://order-service:8000"
    delivery_service_url: str = "http://delivery-service:8000"
    payment_service_url: str = "http://payment-service:8000"
    notification_service_url: str = "http://notification-service:8000"

    rate_limit_default_per_minute: int = 120
    rate_limit_auth_per_minute: int = 10

    # How long a request may wait for a free connection in a backend's pool
    # before the gateway gives up with 503 — short on purpose, see app/proxy.py.
    upstream_pool_timeout_seconds: float = 0.5
    upstream_connect_timeout_seconds: float = 1.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def service_urls(self) -> dict[str, str]:
        """Base URL per backend service."""
        return {
            "user-service": self.user_service_url,
            "restaurant-service": self.restaurant_service_url,
            "order-service": self.order_service_url,
            "delivery-service": self.delivery_service_url,
            "payment-service": self.payment_service_url,
            "notification-service": self.notification_service_url,
        }

    @property
    def service_registry(self) -> dict[str, str]:
        """Maps the first /v1/{resource} path segment to the owning service's
        name (a key of service_urls) — see docs/architecture.md#conventions
        (gateway routes by resource, clients don't need to know which service
        owns what)."""
        return {
            "auth": "user-service",
            "users": "user-service",
            "restaurants": "restaurant-service",
            "orders": "order-service",
            "riders": "delivery-service",
            "payments": "payment-service",
            "notifications": "notification-service",
        }


settings = Settings()  # type: ignore[call-arg]
