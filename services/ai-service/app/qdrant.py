"""The Qdrant client (docs/week4-plan.md B4-D2). One per process, closed on
shutdown by app.main's lifespan."""

from qdrant_client import AsyncQdrantClient

from app.config import settings

client = AsyncQdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout_seconds)


async def check_ready() -> bool:
    try:
        await client.get_collections()
        return True
    except Exception:
        return False
