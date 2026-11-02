"""Text → vectors (docs/week4-plan.md B4-D3): a dense bge-small vector for
meaning and a sparse BM25 vector for exact words (dish names), both from
fastembed's local ONNX models — no API key, no per-call cost.

The models are CPU-bound and blocking, so they run in one worker thread,
never on the event loop (which also serves HTTP and the Kafka consumer).
They load on first use: process start stays fast, and the image ships them
pre-downloaded (Dockerfile), so loading never touches the network."""

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache
from typing import Protocol

from fastembed import SparseTextEmbedding, TextEmbedding

from app import metrics
from app.config import settings


@dataclass(frozen=True)
class Sparse:
    indices: list[int]
    values: list[float]


@dataclass(frozen=True)
class Vectors:
    dense: list[float]
    sparse: Sparse


class Embedder(Protocol):
    async def documents(self, texts: Sequence[str]) -> list[Vectors]: ...
    async def query(self, text: str) -> Vectors: ...


# One thread: the models already use `embedding_threads` cores each, and
# two batches at once would only fight over them.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed")


@cache
def _models() -> tuple[TextEmbedding, SparseTextEmbedding]:
    cache_dir = settings.embedding_cache_dir or None
    return (
        TextEmbedding(
            settings.dense_model, cache_dir=cache_dir, threads=settings.embedding_threads
        ),
        SparseTextEmbedding(settings.sparse_model, cache_dir=cache_dir),
    )


def _embed_documents(texts: list[str]) -> list[Vectors]:
    dense_model, sparse_model = _models()
    dense = dense_model.embed(texts)
    sparse = sparse_model.embed(texts)
    return [
        Vectors(d.tolist(), Sparse(s.indices.tolist(), s.values.tolist()))
        for d, s in zip(dense, sparse, strict=True)
    ]


def _embed_query(text: str) -> Vectors:
    """Queries are embedded differently from documents: bge gets its search
    instruction in front (fastembed doesn't add it), and BM25's query form
    counts each word once (the IDF part comes from the collection)."""
    dense_model, sparse_model = _models()
    d = next(iter(dense_model.query_embed(settings.dense_query_prefix + text)))
    s = next(iter(sparse_model.query_embed(text)))
    return Vectors(d.tolist(), Sparse(s.indices.tolist(), s.values.tolist()))


class FastEmbedder:
    async def documents(self, texts: Sequence[str]) -> list[Vectors]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        with metrics.EMBED_SECONDS.labels("documents").time():
            vectors = await loop.run_in_executor(_executor, _embed_documents, list(texts))
        metrics.EMBEDDED_TEXTS.labels("documents").inc(len(texts))
        return vectors

    async def query(self, text: str) -> Vectors:
        loop = asyncio.get_running_loop()
        with metrics.EMBED_SECONDS.labels("query").time():
            vectors = await loop.run_in_executor(_executor, _embed_query, text)
        metrics.EMBEDDED_TEXTS.labels("query").inc()
        return vectors


def download() -> None:
    """Fetches both models into the cache — run once at image build."""
    _models()


embedder: Embedder = FastEmbedder()
