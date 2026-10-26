"""Step 0 (docs/week2-plan.md): the shared Temporal plumbing — workers on
every task queue, the retry policy, business errors and the data converter —
run against Temporal's time-skipping test server."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from app.worker import WORKERS
from app.workflows.ping import ACTIVITY_QUEUES, WorkerPingWorkflow
from sfo_common.temporal import DATA_CONVERTER, WorkerSpec, run_workers
from sfo_contracts import ACTIVITY_RETRY_POLICY, BusinessErrorType, TaskQueue, business_error
from sfo_contracts.activities import AuthorizePaymentInput, PaymentResult
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment

_TEST_QUEUE = "test-foundations"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(data_converter=DATA_CONVERTER) as env:
        yield env


# The test server and workers are shared by every test here, so the tests
# run on the same (module-scoped) event loop as those fixtures.
pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_ping_reaches_every_service_queue(env: WorkflowEnvironment) -> None:
    # order-service's real workers, plus stand-ins for the other services'
    # (each is just a WorkerSpec on its queue, as in their app/worker.py).
    others = [
        WorkerSpec(TaskQueue.RESTAURANT_ACTIVITIES),
        WorkerSpec(TaskQueue.PAYMENT_ACTIVITIES),
        WorkerSpec(TaskQueue.DELIVERY_ACTIVITIES),
    ]
    workers = asyncio.create_task(run_workers(env.client, [*WORKERS, *others], service_name="t"))
    try:
        results = await env.client.execute_workflow(
            WorkerPingWorkflow.run, id=f"ping-{uuid.uuid4()}", task_queue=TaskQueue.ORDER_SAGA
        )
    finally:
        workers.cancel()
        with pytest.raises(asyncio.CancelledError):
            await workers
    assert [r.task_queue for r in results] == list(ACTIVITY_QUEUES)


# ---------- Retry policy and business errors ----------

_attempts: dict[str, int] = {}


@activity.defn
async def flaky(key: str) -> str:
    """Fails like an infrastructure error on the first two attempts."""
    _attempts[key] = _attempts.get(key, 0) + 1
    if _attempts[key] < 3:
        raise ConnectionError("database unavailable")
    return "ok"


@activity.defn
async def out_of_stock(key: str) -> str:
    _attempts[key] = _attempts.get(key, 0) + 1
    raise business_error(BusinessErrorType.OUT_OF_STOCK, "Burger is out of stock")


@activity.defn
async def always_down(key: str) -> str:
    _attempts[key] = _attempts.get(key, 0) + 1
    raise ConnectionError("database unavailable")


@activity.defn
async def echo_payment(data: AuthorizePaymentInput) -> PaymentResult:
    return PaymentResult(payment_id=data.order_id, status=f"{data.amount + Decimal('0.01')}")


@workflow.defn(sandboxed=False)
class CallActivityWorkflow:
    @workflow.run
    async def run(self, name: str, key: str) -> str:
        return await workflow.execute_activity(
            name,
            key,
            result_type=str,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=ACTIVITY_RETRY_POLICY,
        )


@workflow.defn(sandboxed=False)
class DecimalWorkflow:
    @workflow.run
    async def run(self, data: AuthorizePaymentInput) -> PaymentResult:
        return await workflow.execute_activity(
            echo_payment, data, start_to_close_timeout=timedelta(seconds=5)
        )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def test_worker(env: WorkflowEnvironment) -> AsyncIterator[None]:
    spec = WorkerSpec(
        _TEST_QUEUE,
        workflows=[CallActivityWorkflow, DecimalWorkflow],
        activities=[flaky, out_of_stock, always_down, echo_payment],
    )
    task = asyncio.create_task(run_workers(env.client, [spec], service_name="t"))
    yield
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _call(env: WorkflowEnvironment, name: str, key: str) -> str:
    return await env.client.execute_workflow(
        CallActivityWorkflow.run, args=[name, key], id=key, task_queue=_TEST_QUEUE
    )


async def test_infrastructure_error_is_retried(env: WorkflowEnvironment, test_worker: None) -> None:
    key = str(uuid.uuid4())
    assert await _call(env, "flaky", key) == "ok"
    assert _attempts[key] == 3


async def test_infrastructure_error_gives_up_after_five_attempts(
    env: WorkflowEnvironment, test_worker: None
) -> None:
    key = str(uuid.uuid4())
    with pytest.raises(WorkflowFailureError):
        await _call(env, "always_down", key)
    assert _attempts[key] == 5


async def test_business_error_is_not_retried(env: WorkflowEnvironment, test_worker: None) -> None:
    key = str(uuid.uuid4())
    with pytest.raises(WorkflowFailureError) as exc_info:
        await _call(env, "out_of_stock", key)
    activity_error = exc_info.value.cause
    assert isinstance(activity_error, ActivityError)
    assert isinstance(activity_error.cause, ApplicationError)
    assert activity_error.cause.type == BusinessErrorType.OUT_OF_STOCK
    assert activity_error.cause.non_retryable
    assert _attempts[key] == 1


async def test_decimal_round_trips_through_contracts(
    env: WorkflowEnvironment, test_worker: None
) -> None:
    result = await env.client.execute_workflow(
        DecimalWorkflow.run,
        AuthorizePaymentInput(order_id="o-1", customer_id="c-1", amount=Decimal("1250.10")),
        id=str(uuid.uuid4()),
        task_queue=_TEST_QUEUE,
    )
    assert isinstance(result, PaymentResult)
    assert result.status == "1250.11"
