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

    jwt_secret: str
    jwt_algorithm: str = "HS256"

    mongo_url: str = "mongodb://mongo:27017"
    mongo_db: str = "analytics"
    # Hours of the day (peak-hour load) are counted in this timezone.
    analytics_timezone: str = "Asia/Karachi"


settings = Settings()  # type: ignore[call-arg]
