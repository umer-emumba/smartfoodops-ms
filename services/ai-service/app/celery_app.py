"""ai-service's Celery app (docs/week5-plan.md step 5): restaurant content
is generated in the background, so the owner's request returns at once and
no LLM call ever runs inside an API request for it (Part B PDF: "long-running
… asynchronous execution without blocking core ordering flows").

The worker (`ai-worker`, the same image) runs app/tasks.py. The API process
only enqueues, by task name (`enqueue_content`), and never imports the
tasks."""

from sfo_common.celery import make_celery_app
from sfo_observability import configure_logging, instrument_celery, instrument_celery_metrics

from app.config import settings

GENERATE_CONTENT = f"{settings.service_name}.generate_content"
SWEEP_CONTENT = f"{settings.service_name}.sweep_content"

app = make_celery_app(
    settings.service_name,
    settings.celery_broker_url,
    configure_logging=lambda: configure_logging(settings.service_name, settings.log_level),
)
instrument_celery_metrics()
app.conf.include = ["app.tasks"]
# The trace ID travels in the task message: request (API) → generation
# (worker) is one trace in Jaeger.
instrument_celery()
app.conf.beat_schedule = {
    "sweep-content": {
        "task": SWEEP_CONTENT,
        "schedule": settings.content_sweep_interval_seconds,
        "options": {"expires": settings.content_sweep_interval_seconds},
    }
}


def enqueue_content(draft_id: str) -> None:
    """Blocking (publishes to RabbitMQ) — call from a thread in async code."""
    app.send_task(GENERATE_CONTENT, args=[draft_id])
