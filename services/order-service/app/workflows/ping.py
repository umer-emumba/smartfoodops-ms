import asyncio
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sfo_contracts import ACTIVITY_RETRY_POLICY, TaskQueue, WorkflowName
    from sfo_contracts.activities import PING, PingResult

# Every queue that serves activities — ORDER_SAGA is covered by this
# workflow running at all.
ACTIVITY_QUEUES = (
    TaskQueue.ORDER_ACTIVITIES,
    TaskQueue.RESTAURANT_ACTIVITIES,
    TaskQueue.PAYMENT_ACTIVITIES,
    TaskQueue.DELIVERY_ACTIVITIES,
)


@workflow.defn(name=WorkflowName.WORKER_PING)
class WorkerPingWorkflow:
    """Diagnostic: runs the `ping` activity on every service's task queue,
    proving each worker is polling and can execute activities. Fails within
    30s if one isn't. Run it with:

        docker compose exec temporal temporal workflow execute \\
            --type WorkerPingWorkflow --task-queue order-saga
    """

    @workflow.run
    async def run(self) -> list[PingResult]:
        return list(
            await asyncio.gather(
                *(
                    workflow.execute_activity(
                        PING,
                        task_queue=queue,
                        result_type=PingResult,
                        start_to_close_timeout=timedelta(seconds=5),
                        schedule_to_close_timeout=timedelta(seconds=30),
                        retry_policy=ACTIVITY_RETRY_POLICY,
                    )
                    for queue in ACTIVITY_QUEUES
                )
            )
        )
