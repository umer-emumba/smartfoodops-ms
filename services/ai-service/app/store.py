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
only newer docs go through the model — and a newer doc whose text is the same
(stock, availability) only has its payload replaced."""

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
        "cuisine_words": KEYWORD,
        "words": KEYWORD,
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
        "cuisine_words": KEYWORD,
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
    """`menu_items__bge_base_en_v1_5_r1`."""
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

    async def _stored(self, kind: str, ids: Sequence[str]) -> dict[str, tuple[int, str]]:
        """Each stored point's version and indexed text ("" for a tombstone)."""
        points = await self._client.retrieve(
            name(kind), ids=list(ids), with_payload=["source_version", "text"], with_vectors=False
        )
        return {
            str(p.id): (int(p.payload.get("source_version", -1)), str(p.payload.get("text", "")))
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
        that don't exist). Returns how many were written.

        A doc whose text is unchanged (a stock flip, an availability toggle —
        fields outside the text) gets its payload replaced and keeps its
        vectors: no embedding. If another write changed the point in between,
        the guarded payload write misses and the doc is embedded after all."""
        if not docs:
            return 0
        stored = await self._stored(kind, [d.id for d in docs])
        fresh = [d for d in docs if stored.get(d.id, (-1, ""))[0] < version]
        metrics.INDEX_WRITES.labels(kind, "stale").inc(len(docs) - len(fresh))
        if not fresh:
            return 0
        same_text = [d for d in fresh if d.text and stored.get(d.id, (-1, ""))[1] == d.text]
        to_embed = [d for d in fresh if d not in same_text]
        if same_text:
            missed = await self._replace_payloads(kind, same_text, version)
            metrics.INDEX_WRITES.labels(kind, "payload_only").inc(len(same_text) - len(missed))
            to_embed += missed
        if to_embed:
            await self._client.upsert(
                name(kind),
                points=await self._points(to_embed),
                update_filter=m.Filter(
                    must=[m.FieldCondition(key="source_version", range=m.Range(lt=version))]
                ),
            )
            metrics.INDEX_WRITES.labels(kind, "written").inc(len(to_embed))
        return len(fresh)

    async def _replace_payloads(self, kind: str, docs: Sequence[Doc], version: int) -> list[Doc]:
        """Overwrites each doc's payload in one request, only where the point
        is still older than `version` and still has the same text (so its
        vectors match). Returns the docs that didn't land that way."""
        await self._client.batch_update_points(
            name(kind),
            update_operations=[
                m.OverwritePayloadOperation(
                    overwrite_payload=m.SetPayload(
                        payload=d.payload,
                        filter=m.Filter(
                            must=[
                                m.HasIdCondition(has_id=[d.id]),
                                m.FieldCondition(key="source_version", range=m.Range(lt=version)),
                                m.FieldCondition(key="text", match=m.MatchValue(value=d.text)),
                            ]
                        ),
                    )
                )
                for d in docs
            ],
        )
        after = await self._stored(kind, [d.id for d in docs])
        # Still older: the text changed under us — embed. Newer: someone else
        # won, nothing to do (upsert_newer's own guard would skip it too).
        return [d for d in docs if after.get(d.id, (-1, ""))[0] < version]

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

    async def customer_orders(
        self, customer_id: str, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        """The payloads of the customer's latest `limit` order points (not
        their summary), newest first, and how many orders they have in all —
        so the summary's cost doesn't grow with a long history."""
        only_orders = m.Filter(
            must=[
                m.FieldCondition(key="customer_id", match=m.MatchValue(value=customer_id)),
                m.FieldCondition(key="doc_type", match=m.MatchValue(value="order")),
            ]
        )
        points, _ = await self._client.scroll(
            name(ORDER_HISTORY),
            scroll_filter=only_orders,
            order_by=m.OrderBy(key="delivered_at", direction=m.Direction.DESC),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        total = await self._client.count(name(ORDER_HISTORY), count_filter=only_orders, exact=True)
        return [p.payload for p in points if p.payload is not None], total.count


def default_store() -> Store:
    from app.embedding import embedder

    return Store(qdrant.client, embedder)
