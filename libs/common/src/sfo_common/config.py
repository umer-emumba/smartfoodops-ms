from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common config every service loads. Extend per-service for extra vars.

    Loaded from environment / `.env` via pydantic-settings — see each service's
    `env.example` for the required vars.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str
    environment: str = "local"
    log_level: str = "INFO"

    database_url: str
    redis_url: str = "redis://redis:6379/0"

    # Per replica — total connections against Postgres are
    # replicas x (pool_size + max_overflow). See sfo_common.db.make_engine.
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout_seconds: float = 3.0
    db_statement_timeout_ms: int = 5000

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
