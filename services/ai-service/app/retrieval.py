"""Hybrid retrieval (docs/week4-plan.md step 4): `retrieve(query, filters)`
is the one way into the index — the search API (app/api/routes.py) and
Week 5's assistant both call it.

One Qdrant query per search: a dense (bge) and a sparse (BM25) candidate
list, each already filtered, fused by Reciprocal Rank Fusion. RRF combines
ranks, not raw scores, so the two very different score scales never need
tuning against each other: meaning finds "light vegetarian lunch", BM25
finds "haleem" by its name, and a document both rank highly comes first.

Filters run inside the query, never after it, so a page of results is
never cut short by filtering. What a customer may see is applied here, not
by callers: only active restaurants, only items that are available, in
stock and not deleted."""

from dataclasses import dataclass, field
from typing import Any, Literal

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as m

from app import metrics
from app.config import settings
from app.embedding import Embedder, Vectors
from app.store import MENU_ITEMS, ORDER_HISTORY, RESTAURANTS, REVIEWS, name

Kind = Literal["item", "restaurant", "review", "cuisine"]
_COLLECTION: dict[str, str] = {
    "item": MENU_ITEMS,
    "restaurant": RESTAURANTS,
    "review": REVIEWS,
    "cuisine": MENU_ITEMS,
}
# Payload fields that are bookkeeping, not results.
_INTERNAL = {"text", "source_version", "deleted", "doc_type"}
# Each side of the fusion contributes this many candidates per result asked
# for: enough that something ranked low by one and high by the other still
# makes it into the fused list.
_CANDIDATES_PER_RESULT = 4
# Items shown under each cuisine for kind=cuisine.
_ITEMS_PER_CUISINE = 3


@dataclass(frozen=True)
class Filters:
    kind: Kind = "item"
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None
    max_price: float | None = None
    dietary: tuple[str, ...] = ()
    restaurant_id: str | None = None
    # Order history only: the customer whose orders are searched.
    customer_id: str | None = None
    limit: int = 10


@dataclass(frozen=True)
class Hit:
    kind: str
    id: str
    score: float
    text: str
    data: dict[str, Any] = field(default_factory=dict)


def _match(key: str, value: Any) -> m.FieldCondition:
    return m.FieldCondition(key=key, match=m.MatchValue(value=value))


def catalog_filter(f: Filters) -> m.Filter:
    """What a search may return from the catalog collections, plus the
    caller's own filters."""
    must: list[m.Condition] = [_match("restaurant_status", "active")]
    if f.kind in ("item", "cuisine"):
        must += [_match("is_available", True), _match("in_stock", True)]
        if f.max_price is not None:
            must.append(m.FieldCondition(key="price", range=m.Range(lte=f.max_price)))
        # Every requested tag must be on the item.
        must += [_match("dietary_tags", tag) for tag in f.dietary]
    if f.restaurant_id is not None:
        must.append(_match("restaurant_id", f.restaurant_id))
    if f.lat is not None and f.lng is not None and f.radius_km is not None:
        must.append(
            m.FieldCondition(
                key="location",
                geo_radius=m.GeoRadius(
                    center=m.GeoPoint(lat=f.lat, lon=f.lng), radius=f.radius_km * 1000
                ),
            )
        )
    return m.Filter(must=must, must_not=[_match("deleted", True)])


def history_filter(customer_id: str) -> m.Filter:
    return m.Filter(must=[_match("customer_id", customer_id), _match("doc_type", "order")])


def _prefetch(v: Vectors, query_filter: m.Filter, limit: int) -> list[m.Prefetch]:
    return [
        m.Prefetch(query=v.dense, using="dense", filter=query_filter, limit=limit),
        m.Prefetch(
            query=m.SparseVector(indices=v.sparse.indices, values=v.sparse.values),
            using="bm25",
            filter=query_filter,
            limit=limit,
        ),
    ]


def _fusion() -> m.RrfQuery:
    """Dense first, sparse second — the order `_prefetch` lists them."""
    return m.RrfQuery(
        rrf=m.Rrf(weights=[settings.fusion_dense_weight, settings.fusion_sparse_weight])
    )


def _entity_id(kind: str, payload: dict[str, Any]) -> str:
    key = {
        "item": "menu_item_id",
        "restaurant": "restaurant_id",
        "review": "order_id",
        "order": "order_id",
    }[kind]
    return str(payload.get(key, ""))


def _hit(kind: str, point: m.ScoredPoint) -> Hit:
    payload = point.payload or {}
    return Hit(
        kind=kind,
        id=_entity_id(kind, payload),
        score=point.score,
        text=str(payload.get("text", "")),
        data={k: v for k, v in payload.items() if k not in _INTERNAL},
    )


class Retriever:
    def __init__(self, client: AsyncQdrantClient, embedder: Embedder) -> None:
        self._client = client
        self._embedder = embedder

    async def retrieve(self, query: str, filters: Filters) -> list[Hit]:
        vectors = await self._embedder.query(query)
        if filters.customer_id is not None:
            kind = "order"
            hits = await self._search(
                kind, ORDER_HISTORY, vectors, history_filter(filters.customer_id), filters.limit
            )
        elif filters.kind == "cuisine":
            kind = filters.kind
            with metrics.QUERY_SECONDS.labels(kind).time():
                hits = await self._cuisines(vectors, catalog_filter(filters), filters.limit)
        else:
            kind = filters.kind
            hits = await self._search(
                kind, _COLLECTION[kind], vectors, catalog_filter(filters), filters.limit
            )
        metrics.SEARCHES.labels(kind, "hits" if hits else "empty").inc()
        return hits

    async def _search(
        self, kind: str, collection: str, v: Vectors, query_filter: m.Filter, limit: int
    ) -> list[Hit]:
        with metrics.QUERY_SECONDS.labels(kind).time():
            result = await self._client.query_points(
                name(collection),
                prefetch=_prefetch(v, query_filter, limit * _CANDIDATES_PER_RESULT),
                query=_fusion(),
                limit=limit,
                with_payload=True,
            )
        return [_hit(kind, p) for p in result.points]

    async def _cuisines(self, v: Vectors, query_filter: m.Filter, limit: int) -> list[Hit]:
        """Cuisines ranked by their best-matching dishes: items grouped by
        cuisine, each group scored by its top item."""
        result = await self._client.query_points_groups(
            name(MENU_ITEMS),
            group_by="cuisine",
            prefetch=_prefetch(v, query_filter, limit * _ITEMS_PER_CUISINE * 4),
            query=_fusion(),
            limit=limit,
            group_size=_ITEMS_PER_CUISINE,
            with_payload=True,
        )
        hits = []
        for group in result.groups:
            items = [_hit("item", p) for p in group.hits]
            hits.append(
                Hit(
                    kind="cuisine",
                    id=str(group.id),
                    score=max(p.score for p in group.hits),
                    text=str(group.id),
                    data={"items": [{"id": i.id, "score": i.score, **i.data} for i in items]},
                )
            )
        return hits


def default_retriever() -> Retriever:
    from app import qdrant
    from app.embedding import embedder

    return Retriever(qdrant.client, embedder)


_default: Retriever | None = None


async def retrieve(query: str, filters: Filters) -> list[Hit]:
    """`default_retriever().retrieve`, created on first use."""
    global _default
    if _default is None:
        _default = default_retriever()
    return await _default.retrieve(query, filters)
