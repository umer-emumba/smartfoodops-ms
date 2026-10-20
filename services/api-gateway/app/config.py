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

    # Where this service gets the public keys that verify access tokens
    # (user-service's JWKS; see sfo_auth.keys). Public keys only: this
    # service can verify a token but never issue one.
    jwt_jwks_url: str = "http://user-service:8000/.well-known/jwks.json"

    cors_origins: str = ""

    user_service_url: str = "http://user-service:8000"
    restaurant_service_url: str = "http://restaurant-service:8000"
    order_service_url: str = "http://order-service:8000"
    delivery_service_url: str = "http://delivery-service:8000"
    payment_service_url: str = "http://payment-service:8000"
    notification_service_url: str = "http://notification-service:8000"
    tracking_service_url: str = "http://tracking-service:8000"
    analytics_service_url: str = "http://analytics-service:8000"

    rate_limit_default_per_minute: int = 120
    rate_limit_auth_per_minute: int = 10

    # How long a request may wait for a free connection in a backend's pool
    # before the gateway gives up with 503 — short on purpose, see app/proxy.py.
    # Largest request body the gateway will accept (413 above it). Every API
    # body is small JSON; this stops one oversized request from holding a
    # pod's memory, since the gateway buffers bodies before forwarding.
    max_request_body_bytes: int = 1_048_576

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
            "tracking-service": self.tracking_service_url,
            "analytics-service": self.analytics_service_url,
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
            "deliveries": "delivery-service",
            "payments": "payment-service",
            "notifications": "notification-service",
            "tracking": "tracking-service",
            "analytics": "analytics-service",
        }


settings = Settings()  # type: ignore[call-arg]
