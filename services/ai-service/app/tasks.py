"""ai-worker's tasks (docs/week5-plan.md step 5).

`generate_content` makes one attempt at a draft (app/content/service.py);
the draft row, not Celery, counts attempts, so the limit holds however many
task messages exist for it. A transient LLM error leaves it pending and the
task retries with a backoff. `sweep_content` re-queues drafts left pending
— the API commits, then enqueues, and a crash or broker outage in between
would otherwise strand them."""

import logging

from celery import Task
from sfo_common.celery import run_async

from app.celery_app import GENERATE_CONTENT, SWEEP_CONTENT, app
from app.config import settings
from app.content import service
from app.db import SessionFactory
from app.llm import llm

logger = logging.getLogger(__name__)


@app.task(  # type: ignore[untyped-decorator]
    name=GENERATE_CONTENT, bind=True, acks_late=True, reject_on_worker_lost=True, max_retries=None
)
def generate_content(self: Task, draft_id: str) -> None:
    status = run_async(service.generate(SessionFactory, llm, draft_id))
    if status == "pending":
        raise self.retry(countdown=min(30 * 2**self.request.retries, 300))


@app.task(name=SWEEP_CONTENT)  # type: ignore[untyped-decorator]
def sweep_content() -> None:
    ids = run_async(service.stale_pending(SessionFactory, settings.content_sweep_after_seconds))
    for draft_id in ids:
        generate_content.delay(draft_id)
    if ids:
        logger.warning("Re-queued %d stale pending content drafts", len(ids))
