from typing import Literal

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


class TemporalSettings(BaseSettings):
    """Mixed into the Settings of services that run a Temporal worker
    (order, restaurant, payment, delivery) — see sfo_common.temporal."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "default"
    # embedded: the worker runs inside the API process, started from its
    # lifespan (development). standalone: the API doesn't start it; run
    # `python -m app.worker` as its own process from the same image
    # (production — API and workers get their own CPU and scale separately).
    temporal_worker_mode: Literal["embedded", "standalone"] = "embedded"


class OutboxRelaySettings(BaseSettings):
    """Mixed into the Settings of services whose Celery worker relays their
    outbox to Kafka (order, payment, delivery) — see sfo_events.relay and
    docs/week3-plan.md W3-D1/W3-D2."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kafka_bootstrap_servers: str = "kafka:9092"
    schema_registry_url: str = "http://schema-registry:8081"
    # Only the worker needs it (the API never talks to RabbitMQ); compose
    # sets it on the worker containers.
    celery_broker_url: str = ""
    outbox_relay_interval_seconds: float = 1.0
    outbox_relay_batch_size: int = 200
    outbox_retention_days: int = 7
