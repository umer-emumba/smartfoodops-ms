"""restaurant-service's Temporal worker (docs/week2-plan.md D1, D2): the order
saga's restaurant activities, on `restaurant-activities`. Started from app.main's
lifespan in embedded mode; run as its own process with
`python -m app.worker` in standalone mode."""

from sfo_common.temporal import WorkerSpec, run_standalone
from sfo_contracts import TaskQueue
from sfo_observability import configure_logging

from app.config import settings

WORKERS = [WorkerSpec(TaskQueue.RESTAURANT_ACTIVITIES)]

if __name__ == "__main__":
    configure_logging(settings.service_name, settings.log_level)
    run_standalone(WORKERS, settings, service_name=settings.service_name)
