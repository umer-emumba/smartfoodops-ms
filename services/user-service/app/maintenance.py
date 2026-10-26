import asyncio
import logging
from datetime import UTC, datetime

from sfo_observability import quiet
from sqlalchemy import text

from app import repository
from app.config import settings
from app.db import SessionFactory

_logger = logging.getLogger(__name__)

# Distinct from sfo_common.db's migration lock key.
_PURGE_LOCK_KEY = 727_002
_BATCH_SIZE = 1000


async def purge_expired_refresh_tokens_once() -> int:
    """Deletes expired refresh-token rows in small batches, one short
    transaction each. Every replica runs this loop; the transaction-scoped
    try-lock means only one of them deletes at a time and the others skip
    the run instead of queueing."""
    total = 0
    while True:
        async with SessionFactory() as session:
            got_lock = (
                await session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _PURGE_LOCK_KEY}
                )
            ).scalar_one()
            if not got_lock:
                return total
            deleted = await repository.purge_expired_refresh_tokens(
                session, now=datetime.now(UTC), batch_size=_BATCH_SIZE
            )
            await session.commit()
        total += deleted
        if deleted < _BATCH_SIZE:
            return total


async def run_refresh_token_purge_loop() -> None:
    """Started from app.main's lifespan. Week 3's Celery beat can take this
    over; until then it runs in-process."""
    while True:
        await asyncio.sleep(settings.refresh_token_purge_interval_seconds)
        try:
            with quiet():
                deleted = await purge_expired_refresh_tokens_once()
            if deleted:
                _logger.info(
                    "Purged expired refresh tokens", extra={"extra_fields": {"count": deleted}}
                )
        except Exception:
            # Housekeeping must never take the service down; retry next run.
            _logger.warning("Refresh-token purge failed", exc_info=True)
