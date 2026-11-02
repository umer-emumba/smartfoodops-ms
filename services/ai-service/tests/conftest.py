"""Fixtures for the `db`-marked tests: the compose Qdrant (published on
localhost:6333), throwaway collections per test, and a fake embedder — the
tests are about what gets written when, not about the model. Run with
`scripts/check.sh --with-db` (or `pytest -m db`) while `docker compose up`."""

import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from app.embedding import Sparse, Vectors

QDRANT_URL = os.environ.get("AI_TEST_QDRANT_URL", "http://localhost:6333")


class FakeEmbedder:
    """Deterministic vectors from the text's hash; counts what it embedded."""

    def __init__(self) -> None:
        self.embedded: list[str] = []

    @staticmethod
    def _vectors(text: str) -> Vectors:
        digest = hashlib.sha256(text.encode()).digest()
        dense = [((digest[i % 32] + i) % 17) / 17 + 0.01 for i in range(384)]
        words = sorted({hash(w) % 10_000 for w in text.lower().split()})
        return Vectors(dense, Sparse(words, [1.0] * len(words)))

    async def documents(self, texts: Sequence[str]) -> list[Vectors]:
        self.embedded.extend(texts)
        return [self._vectors(t) for t in texts]

    async def query(self, text: str) -> Vectors:
        return self._vectors(text)


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest_asyncio.fixture
async def store(embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator:
    from app import store as store_module
    from app.config import settings
    from qdrant_client import AsyncQdrantClient

    # A revision no real index uses: collections of their own, dropped after.
    monkeypatch.setattr(settings, "index_revision", 9000 + uuid.uuid4().int % 1000)
    client = AsyncQdrantClient(url=QDRANT_URL, timeout=10)
    s = store_module.Store(client, embedder)
    await s.ensure_collections()
    yield s
    for kind in (*store_module.CATALOG, store_module.ORDER_HISTORY):
        await client.delete_collection(store_module.name(kind))
    await client.close()
