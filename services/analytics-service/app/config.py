from pydantic_settings import SettingsConfigDict
from sfo_common.config import KafkaConsumerSettings


class Settings(KafkaConsumerSettings):
    """analytics-service has no Postgres (docs/week3-plan.md W3-D9), so it
    doesn't inherit sfo_common.BaseServiceSettings, which requires
    DATABASE_URL."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "analytics-service"
    environment: str = "local"
    log_level: str = "INFO"

    # Where this service gets the public keys that verify access tokens
    # (user-service's JWKS; see sfo_auth.keys). Public keys only: this
    # service can verify a token but never issue one.
    jwt_jwks_url: str = "http://user-service:8000/.well-known/jwks.json"

    mongo_url: str = "mongodb://mongo:27017"
    mongo_db: str = "analytics"
    # Hours of the day (peak-hour load) are counted in this timezone.
    analytics_timezone: str = "Asia/Karachi"


settings = Settings()  # type: ignore[call-arg]
