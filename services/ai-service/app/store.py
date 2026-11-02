"""The Qdrant side of the index (docs/week4-plan.md B4-D2, B4-D5, B4-D6).

Four collections, each with a dense vector (`dense`, cosine) and a sparse
BM25 vector (`bm25`, IDF computed by Qdrant) for hybrid search. Their names
carry the dense model and document revision (`settings.index_suffix`), so a
model change builds a fresh index beside the old one.

Writes are safe to repeat and to race:
- catalog points (items, restaurants, reviews) are only overwritten by a
  newer `source_version` — one conditional upsert (`update_filter`), so a
  backfill and live events can interleave in any order;
- order points never change once written (insert-only);
- a deleted menu item stays as a tombstone (no vectors, `deleted: true`)
  so an older upsert can't bring it back.
Unchanged documents aren't re-embedded: stored versions are read first and
only newer docs go through the model."""

from collections.abc import Sequence
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as m

from app import metrics, qdrant
from app.config import settings
from app.documents import Doc
from app.embedding import Embedder

MENU_ITEMS = "menu_items"
RESTAURANTS = "restaurants"
REVIEWS = "reviews"
ORDER_HISTORY = "order_history"
CATALOG = (MENU_ITEMS, RESTAURANTS, REVIEWS)

KEYWORD = m.PayloadSchemaType.KEYWORD
BOOL = m.PayloadSchemaType.BOOL
INTEGER = m.PayloadSchemaType.INTEGER
FLOAT = m.PayloadSchemaType.FLOAT
GEO = m.PayloadSchemaType.GEO
DATETIME = m.PayloadSchemaType.DATETIME
# The fields searches filter on (step 4), indexed so filtering stays cheap.
_INDEXES: dict[str, dict[str, m.PayloadSchemaType]] = {
    MENU_ITEMS: {
        "restaurant_id": KEYWORD,
        "restaurant_status": KEYWORD,
        "city": KEYWORD,
        "cuisine": KEYWORD,
        "location": GEO,
        "price": FLOAT,
        "dietary_tags": KEYWORD,
        "spice_level": INTEGER,
        "is_available": BOOL,
        "in_stock": BOOL,
        "deleted": BOOL,
        "source_version": INTEGER,
    },
    RESTAURANTS: {
        "restaurant_id": KEYWORD,
        "restaurant_status": KEYWORD,
        "city": KEYWORD,
        "cuisine": KEYWORD,
        "location": GEO,
        "avg_rating": FLOAT,
        "deleted": BOOL,
        "source_version": INTEGER,
    },
    REVIEWS: {
        "restaurant_id": KEYWORD,
        "restaurant_status": KEYWORD,
        "city": KEYWORD,
        "location": GEO,
        "rating": INTEGER,
        "order_id": KEYWORD,
        "deleted": BOOL,
        "source_version": INTEGER,
    },
    ORDER_HISTORY: {
        "customer_id": KEYWORD,
        "doc_type": KEYWORD,
        "restaurant_id": KEYWORD,
        "delivered_at": DATETIME,
    },
}


def name(kind: str) -> str:
    """`menu_items__bge_small_en_v1_5_r1`."""
    return f"{kind}__{settings.index_suffix}"


class Store:
    def __init__(self, client: AsyncQdrantClient, embedder: Embedder) -> None:
        self._client = client
        self._embedder = embedder
        self._ready = False

    async def ensure_collections(self) -> None:
        """Creates missing collections and their payload indexes. Cheap after
        the first success in this process."""
        if self._ready:
            return
        for kind, indexes in _INDEXES.items():
            collection = name(kind)
            if not await self._client.collection_exists(collection):
                await self._client.create_collection(
                    collection,
                    vectors_config={
                        "dense": m.VectorParams(size=settings.dense_dim, distance=m.Distance.COSINE)
                    },
                    sparse_vectors_config={"bm25": m.SparseVectorParams(modifier=m.Modifier.IDF)},
                )
            for field, schema in indexes.items():
                await self._client.create_payload_index(collection, field, schema)
        self._ready = True

    async def _stored_versions(self, kind: str, ids: Sequence[str]) -> dict[str, int]:
        points = await self._client.retrieve(
            name(kind), ids=list(ids), with_payload=["source_version"], with_vectors=False
        )
        return {
            str(p.id): int(p.payload.get("source_version", -1))
            for p in points
            if p.payload is not None
        }

    async def _points(self, docs: Sequence[Doc]) -> list[m.PointStruct]:
        """Vectors for docs with text; a tombstone (no text) gets none."""
        texts = [d for d in docs if d.text]
        vectors = dict(
            zip(
                [d.id for d in texts],
                await self._embedder.documents([d.text for d in texts]),
                strict=True,
            )
        )
        points = []
        for doc in docs:
            v = vectors.get(doc.id)
            vector: dict[str, Any] = (
                {
                    "dense": v.dense,
                    "bm25": m.SparseVector(indices=v.sparse.indices, values=v.sparse.values),
                }
                if v is not None
                else {}
            )
            points.append(m.PointStruct(id=doc.id, vector=vector, payload=doc.payload))
        return points

    async def upsert_newer(self, kind: str, docs: Sequence[Doc], version: int) -> int:
        """Writes the docs whose stored version is older than `version` (or
        that don't exist). Returns how many were written."""
        if not docs:
            return 0
        stored = await self._stored_versions(kind, [d.id for d in docs])
        fresh = [d for d in docs if stored.get(d.id, -1) < version]
        metrics.INDEX_WRITES.labels(kind, "stale").inc(len(docs) - len(fresh))
        if not fresh:
            return 0
        await self._client.upsert(
            name(kind),
            points=await self._points(fresh),
            update_filter=m.Filter(
                must=[m.FieldCondition(key="source_version", range=m.Range(lt=version))]
            ),
        )
        metrics.INDEX_WRITES.labels(kind, "written").inc(len(fresh))
        return len(fresh)

    async def set_restaurant_fields(
        self, kinds: Sequence[str], restaurant_id: str, fields: dict[str, Any], version: int
    ) -> None:
        """Sets `fields` on every point of the restaurant older than
        `version` (and moves them to it) — no re-embedding, for changes
        outside the text: status, rating."""
        for kind in kinds:
            await self._client.set_payload(
                name(kind),
                payload={**fields, "source_version": version},
                points=m.Filter(
                    must=[
                        m.FieldCondition(
                            key="restaurant_id", match=m.MatchValue(value=restaurant_id)
                        ),
                        m.FieldCondition(key="source_version", range=m.Range(lt=version)),
                    ]
                ),
            )

    async def insert_new(self, kind: str, docs: Sequence[Doc]) -> int:
        """Writes only docs whose point doesn't exist yet (immutable ones)."""
        if not docs:
            return 0
        existing = await self._client.retrieve(
            name(kind), ids=[d.id for d in docs], with_payload=False, with_vectors=False
        )
        have = {str(p.id) for p in existing}
        new = [d for d in docs if d.id not in have]
        metrics.INDEX_WRITES.labels(kind, "stale").inc(len(docs) - len(new))
        metrics.INDEX_WRITES.labels(kind, "written").inc(len(new))
        if new:
            await self._client.upsert(
                name(kind), points=await self._points(new), update_mode=m.UpdateMode.INSERT_ONLY
            )
        return len(new)

    async def upsert(self, kind: str, docs: Sequence[Doc]) -> None:
        """Unconditional — for derived docs rebuilt from scratch each time."""
        if docs:
            await self._client.upsert(name(kind), points=await self._points(docs))

    async def payload(self, kind: str, point_id: str) -> dict[str, Any] | None:
        [*found] = await self._client.retrieve(
            name(kind), ids=[point_id], with_payload=True, with_vectors=False
        )
        return found[0].payload if found else None

    async def customer_orders(self, customer_id: str) -> list[dict[str, Any]]:
        """Payloads of the customer's order points (not their summary)."""
        out: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            points, offset = await self._client.scroll(
                name(ORDER_HISTORY),
                scroll_filter=m.Filter(
                    must=[
                        m.FieldCondition(key="customer_id", match=m.MatchValue(value=customer_id)),
                        m.FieldCondition(key="doc_type", match=m.MatchValue(value="order")),
                    ]
                ),
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            out.extend(p.payload for p in points if p.payload is not None)
            if offset is None:
                return out


def default_store() -> Store:
    from app.embedding import embedder

    return Store(qdrant.client, embedder)
