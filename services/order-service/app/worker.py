"""order-service's Temporal workers (docs/week2-plan.md D1, D2): the saga
workflows on `order-saga`, the activities that change orders on
`order-activities`, and the reconciler (app/reconciler.py) alongside them.
Started from app.main's lifespan in embedded mode; run as its own process
with `python -m app.worker` in standalone mode."""

from sfo_common.temporal import WorkerSpec, run_standalone
from sfo_contracts import TaskQueue
from sfo_observability import configure_logging

from app.activities import ACTIVITIES
from app.config import settings
from app.reconciler import run_reconciler
from app.workflows.order_saga import OrderSagaWorkflow
from app.workflows.ping import WorkerPingWorkflow
from app.workflows.refund import OrderRefundWorkflow

WORKERS = [
    WorkerSpec(
        TaskQueue.ORDER_SAGA,
        workflows=[OrderSagaWorkflow, OrderRefundWorkflow, WorkerPingWorkflow],
    ),
    WorkerSpec(TaskQueue.ORDER_ACTIVITIES, activities=ACTIVITIES),
]
BACKGROUND = [run_reconciler]

if __name__ == "__main__":
    configure_logging(settings.service_name, settings.log_level)
    run_standalone(WORKERS, settings, service_name=settings.service_name, background=BACKGROUND)
