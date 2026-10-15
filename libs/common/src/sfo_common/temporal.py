"""Temporal client and worker plumbing shared by the services that run a
worker (docs/week2-plan.md step 0). Needs the `temporal` extra
(`sfo-common[temporal]`).

Each service declares its workers in `app/worker.py` as a list of
WorkerSpec, then either starts them inside the API process
(`embedded_workers`, from the lifespan — TEMPORAL_WORKER_MODE=embedded) or
runs them as their own process (`run_standalone`, via
`python -m app.worker` — TEMPORAL_WORKER_MODE=standalone)."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sfo_contracts import order_workflow_id
from sfo_contracts.activities import PING, PingResult
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from sfo_common.config import TemporalSettings
from sfo_common.errors import ConflictError, ServiceUnavailableError

_logger = logging.getLogger(__name__)

# Pydantic instead of Temporal's default JSON converter: Decimal (money) and
# datetime fields in sfo_contracts' dataclasses round-trip with their types.
# Every client — and so every worker built from it — must use the same one.
DATA_CONVERTER = pydantic_data_converter

# How long in-flight activities get to finish on shutdown before they're
# cancelled (and retried elsewhere). Below Docker's 10s stop timeout.
GRACEFUL_SHUTDOWN_TIMEOUT = timedelta(seconds=8)

# Modules workflows share with the host instead of re-importing inside the
# sandbox: the contracts are pure data and the pydantic converter's modules
# are deterministic — re-importing them per workflow run would only cost time.
_WORKFLOW_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "sfo_contracts",
        "pydantic",
        "pydantic_core",
        "annotated_types",
        # TracingInterceptor runs inside workflows; the sandbox would
        # otherwise refuse to import OpenTelemetry (week3-plan step 4).
        "opentelemetry",
        # Each service's `app/__init__.py` sets up tracing from its settings,
        # and the sandbox re-runs it when a workflow imports from `app`:
        # loading settings reads `.env`, which workflows may not do. Both are
        # already loaded in the host and change nothing at run time.
        "sfo_observability",
        "app.config",
    )
)

# Traces through Temporal (week3-plan W3-D12): starting or signalling a
# workflow, the workflow, and each activity become spans of the caller's
# trace. Set on the client; workers built from it inherit it. A no-op while
# tracing is off (no tracer provider set).
_INTERCEPTORS = [TracingInterceptor()]

_CONNECT_RETRY_MAX_DELAY_SECONDS = 10.0
_EMBEDDED_RESTART_DELAY_SECONDS = 5.0


# A long-running job that runs alongside a service's workers (same process,
# same client), e.g. order-service's reconciler. Cancelled on shutdown.
BackgroundTask = Callable[[Client], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class WorkerSpec:
    """One worker: the workflows and activities it registers on one task
    queue. Every worker also gets a `ping` activity (see _ping_activity)."""

    task_queue: str
    workflows: Sequence[type] = ()
    activities: Sequence[Callable[..., Any]] = ()


async def connect_client(address: str, *, namespace: str = "default") -> Client:
    """Connects to Temporal, retrying with backoff (1s doubling to 10s) until
    it's reachable — services may start before the Temporal server is up."""
    delay = 1.0
    while True:
        try:
            return await Client.connect(
                address,
                namespace=namespace,
                data_converter=DATA_CONVERTER,
                interceptors=_INTERCEPTORS,
            )
        except Exception as exc:
            _logger.warning(
                "Temporal not reachable, retrying",
                extra={"extra_fields": {"address": address, "error": str(exc), "retry_in": delay}},
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _CONNECT_RETRY_MAX_DELAY_SECONDS)


async def lazy_client(settings: TemporalSettings) -> Client:
    """For API processes that start or signal workflows: returns at once and
    connects on first use, so the API comes up (and serves everything else)
    even while Temporal is down; calls made then fail with an RPC error."""
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=DATA_CONVERTER,
        interceptors=_INTERCEPTORS,
        lazy=True,
    )


async def signal_order_saga(client: Client, order_id: str, signal: str, *args: Any) -> None:
    """Signals workflow `order-{order_id}` from an API endpoint. The caller
    has checked the current state for a fast 409; the saga re-checks it, since
    things can move on before the signal arrives."""
    try:
        await client.get_workflow_handle(order_workflow_id(order_id)).signal(signal, *args)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            # Workflow finished, or (before the reconciler restarts it) never started.
            raise ConflictError("This order is not being processed right now") from exc
        raise ServiceUnavailableError("Order workflow service is unavailable") from exc


def _ping_activity(service_name: str, task_queue: str) -> Callable[[], Any]:
    @activity.defn(name=PING)
    async def ping() -> PingResult:
        return PingResult(service=service_name, task_queue=task_queue)

    return ping


async def run_workers(
    client: Client,
    specs: Sequence[WorkerSpec],
    *,
    service_name: str,
    background: Sequence[BackgroundTask] = (),
) -> None:
    """Runs one worker per spec (plus any background tasks) until cancelled,
    then shuts them all down gracefully. Raises if any of them fails."""
    workers = [
        Worker(
            client,
            task_queue=spec.task_queue,
            workflows=list(spec.workflows),
            activities=[*spec.activities, _ping_activity(service_name, spec.task_queue)],
            workflow_runner=_WORKFLOW_RUNNER,
            graceful_shutdown_timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
        )
        for spec in specs
    ]
    runs = [asyncio.create_task(worker.run()) for worker in workers]
    jobs = [asyncio.create_task(task(client)) for task in background]
    _logger.info(
        "Temporal workers started",
        extra={"extra_fields": {"task_queues": [spec.task_queue for spec in specs]}},
    )
    try:
        # Only returns if a worker fails; otherwise waits until cancelled.
        done, _ = await asyncio.wait([*runs, *jobs], return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        # Explicit shutdown (rather than cancelling run()) is what lets
        # in-flight activities finish within GRACEFUL_SHUTDOWN_TIMEOUT.
        await asyncio.gather(*(worker.shutdown() for worker in workers), return_exceptions=True)
        await asyncio.gather(*runs, return_exceptions=True)
        _logger.info("Temporal workers stopped")


async def _run_workers_forever(
    specs: Sequence[WorkerSpec],
    settings: TemporalSettings,
    service_name: str,
    background: Sequence[BackgroundTask],
) -> None:
    while True:
        try:
            client = await connect_client(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            await run_workers(client, specs, service_name=service_name, background=background)
        except Exception:
            # The API keeps serving; the worker comes back on its own.
            _logger.exception("Temporal worker failed, restarting")
            await asyncio.sleep(_EMBEDDED_RESTART_DELAY_SECONDS)


@contextlib.asynccontextmanager
async def embedded_workers(
    specs: Sequence[WorkerSpec],
    settings: TemporalSettings,
    *,
    service_name: str,
    background: Sequence[BackgroundTask] = (),
) -> AsyncIterator[None]:
    """For the API's lifespan: in `embedded` mode, runs the workers as a
    background task (restarted if they fail) and stops them on shutdown. A
    no-op in `standalone` mode, where `python -m app.worker` runs them."""
    if settings.temporal_worker_mode != "embedded":
        yield
        return
    task = asyncio.create_task(_run_workers_forever(specs, settings, service_name, background))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def run_standalone(
    specs: Sequence[WorkerSpec],
    settings: TemporalSettings,
    *,
    service_name: str,
    background: Sequence[BackgroundTask] = (),
) -> None:
    """Entry point for `python -m app.worker`. Runs until SIGTERM/SIGINT,
    then shuts down gracefully. A fatal worker error exits the process so the
    orchestrator restarts it."""

    async def main() -> None:
        main_task = asyncio.current_task()
        assert main_task is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, main_task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            client = await connect_client(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            await run_workers(client, specs, service_name=service_name, background=background)

    asyncio.run(main())
