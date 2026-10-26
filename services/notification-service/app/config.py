from sfo_common import BaseServiceSettings
from sfo_common.config import CelerySettings, KafkaConsumerSettings


class Settings(BaseServiceSettings, KafkaConsumerSettings, CelerySettings):
    # Simulated email/SMS providers (app/senders.py): the share of sends that
    # fail, so Celery retries are exercised. In-app never fails.
    notify_fake_failure_rate: float = 0.1
    # A notification is tried this many times, with exponential backoff
    # (2s, 4s, 8s, ... capped), then marked `failed` (week3-plan W3-D7).
    notify_max_attempts: int = 5
    notify_retry_backoff_seconds: float = 2.0
    notify_retry_backoff_max_seconds: float = 60.0
    # Re-enqueues `pending` notifications whose send was never queued (a
    # crash or RabbitMQ outage between the consumer's commit and the
    # enqueue) or whose task was lost.
    notify_sweep_interval_seconds: float = 30.0
    notify_sweep_min_age_seconds: float = 60.0


settings = Settings()  # type: ignore[call-arg]
