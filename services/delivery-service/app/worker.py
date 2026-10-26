"""delivery-service's Temporal worker (docs/week2-plan.md D1, D2): the order
saga's delivery activities, on `delivery-activities`, the stale-rider
detector (app/stale_riders.py) and the rider-pool gauge
(app/rider_pool_metrics.py) alongside it. Started from app.main's
lifespan in embedded mode; run as its own process with
`python -m app.worker` in standalone mode."""

from sfo_common.temporal import WorkerSpec, run_standalone
from sfo_contracts import TaskQueue
from sfo_observability import configure_logging

from app.activities import ACTIVITIES
from app.config import settings
from app.rider_pool_metrics import run_rider_pool_gauge
from app.stale_riders import run_stale_rider_detector

WORKERS = [WorkerSpec(TaskQueue.DELIVERY_ACTIVITIES, activities=ACTIVITIES)]
# stale_rider_detector (every 30s): a rider on a delivery who stopped sending
# their location is reported to the saga as dropped out, so it finds someone else.
# rider_pool_gauge (every 15s): counts riders by status for Grafana.
BACKGROUND = [run_stale_rider_detector, run_rider_pool_gauge]

if __name__ == "__main__":
    configure_logging(settings.service_name, settings.log_level)
    run_standalone(WORKERS, settings, service_name=settings.service_name, background=BACKGROUND)
