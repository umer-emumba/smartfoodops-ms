"""Celery plumbing shared by the services that run a Celery worker (outbox
relays, notification sending — docs/week3-plan.md W3-D1, W3-D7). Needs the
`celery` extra (`sfo-common[celery]`).

Each service's worker is the same image as its API with a different
command: `celery -A app.celery_app worker --beat --pool=solo --without-mingle
--without-gossip`.

Our DB layer is async-only, but Celery tasks are sync (W3-D2a). `run_async`
runs a coroutine on one event loop kept for the life of the worker process,
so the async engine and its connection pool are reused across tasks instead
of being rebuilt every tick."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from celery import Celery
from celery.signals import setup_logging

_loop: asyncio.AbstractEventLoop | None = None


def make_celery_app(
    service_name: str, broker_url: str, *, configure_logging: Callable[[], None]
) -> Celery:
    """One Celery app per service. Its tasks go to a queue named after the
    service, so workers sharing the RabbitMQ broker only ever receive their
    own tasks."""
    if not broker_url:
        raise RuntimeError("CELERY_BROKER_URL is not set")
    app = Celery(service_name, broker=broker_url)
    app.conf.update(
        task_default_queue=service_name,
        task_ignore_result=True,
        timezone="UTC",
        broker_connection_retry_on_startup=True,
        # The container runs as non-root; /workspace isn't writable.
        beat_schedule_filename="/tmp/celerybeat-schedule",
        worker_hijack_root_logger=False,
        # Remote control (`celery inspect`/`control`) declares transient,
        # non-exclusive queues, which RabbitMQ 4.x refuses. Nothing here
        # uses it; the worker command also turns off mingle and gossip,
        # which need it.
        worker_enable_remote_control=False,
    )

    # Connecting any receiver stops Celery from installing its own
    # plain-text logging — the service's JSON logging applies instead.
    @setup_logging.connect(weak=False)
    def _setup_logging(**_: Any) -> None:
        configure_logging()
        # Per-tick INFO lines (task sent, received, succeeded; and expired
        # ticks discarded while a slow one ran) — about 3 a second per
        # worker at a 1s relay interval. Task failures still log (ERROR), and
        # the tasks log their own outcome when they did work.
        for name in (
            "celery.beat",
            "celery.worker.strategy",
            "celery.worker.request",
            "celery.app.trace",
        ):
            logging.getLogger(name).setLevel(logging.WARNING)

    return app


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Runs `coro` to completion on this process's event loop (created on
    first use: with `--pool=solo` there is no child process, so Celery's
    worker_process_init never fires)."""
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop.run_until_complete(coro)
