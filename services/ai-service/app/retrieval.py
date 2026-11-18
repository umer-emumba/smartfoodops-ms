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
stock and not deleted.

Restaurant suggestions (Week 5 step 1, `suggest_restaurants`) rank
restaurants by their best dishes for the query, under the same item filters,
then weigh in each restaurant's rating and its kitchen's load right now."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as m

from app import metrics
from app.config import settings
from app.documents import point_id, words
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
_INTERNAL = {"text", "source_version", "deleted", "doc_type", "words", "cuisine_words"}
# Each side of the fusion contributes this many candidates per result asked
# for: enough that something ranked low by one and high by the other still
# makes it into the fused list.
_CANDIDATES_PER_RESULT = 4
# Items shown under each cuisine for kind=cuisine.
_ITEMS_PER_CUISINE = 3
# min_rating on dishes: the restaurants rated high enough are looked up
# first (one scroll), then the dishes are searched within them. Past this
# many, the lookup is cut off — far beyond the seeded catalog; a bigger one
# would copy the rating onto each dish point instead.
_MAX_RATED_RESTAURANTS = 1000

# Restaurant suggestions. Rating: a Bayesian average pulled towards
# RATING_PRIOR by RATING_PRIOR_WEIGHT imaginary reviews, so one 5-star
# review doesn't beat fifty 4.6s; it moves the score by at most 40%.
RATING_PRIOR = 3.5
RATING_PRIOR_WEIGHT = 5
RATING_SHARE = 0.4
# Load: an idle kitchen keeps its score, a nearly full one loses up to half;
# a full one, or one not taking orders, isn't suggested.
LOAD_SHARE = 0.5
BUSY_AT = 0.8
_DISHES_PER_RESTAURANT = 3


@dataclass(frozen=True)
class Filters:
    kind: Kind = "item"
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None
    max_price: float | None = None
    dietary: tuple[str, ...] = ()
    restaurant_id: str | None = None
    # 0 not spicy … 3 very spicy (app/documents.py SPICE).
    max_spice: int | None = None
    # Any of these words in the cuisine label: "thai" matches "Thai / Pan-Asian".
    cuisines: tuple[str, ...] = ()
    # None of these words in a dish's name, category or description.
    exclude: tuple[str, ...] = ()
    # The restaurant's average rating (unrated restaurants don't qualify).
    min_rating: float | None = None
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


def _any(key: str, values: Sequence[str]) -> m.FieldCondition:
    return m.FieldCondition(key=key, match=m.MatchAny(any=list(values)))


def catalog_filter(f: Filters, restaurant_ids: Sequence[str] | None = None) -> m.Filter:
    """What a search may return from the catalog collections, plus the
    caller's own filters. `restaurant_ids`: only these restaurants (the
    min_rating lookup's result for dishes)."""
    must: list[m.Condition] = [_match("restaurant_status", "active")]
    must_not: list[m.Condition] = [_match("deleted", True)]
    if f.kind in ("item", "cuisine"):
        must += [_match("is_available", True), _match("in_stock", True)]
        if f.max_price is not None:
            must.append(m.FieldCondition(key="price", range=m.Range(lte=f.max_price)))
        # Every requested tag must be on the item.
        must += [_match("dietary_tags", tag) for tag in f.dietary]
        if f.max_spice is not None:
            must.append(m.FieldCondition(key="spice_level", range=m.Range(lte=f.max_spice)))
        if excluded := words(*f.exclude):
            must_not.append(_any("words", excluded))
    if f.kind in ("item", "cuisine", "restaurant") and (cuisines := words(*f.cuisines)):
        must.append(_any("cuisine_words", cuisines))
    if f.kind == "restaurant" and f.min_rating is not None:
        must.append(m.FieldCondition(key="avg_rating", range=m.Range(gte=f.min_rating)))
    if restaurant_ids is not None:
        must.append(_any("restaurant_id", restaurant_ids))
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
    return m.Filter(must=must, must_not=must_not)


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
        rrf=m.Rrf(
            k=settings.fusion_k,
            weights=[settings.fusion_dense_weight, settings.fusion_sparse_weight],
        )
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


@dataclass(frozen=True)
class Load:
    """A kitchen's load right now (restaurant-service, app/restaurant_client.py)."""

    active_orders: int
    max_orders: int
    accepting: bool

    @property
    def utilization(self) -> float:
        return min(self.active_orders / self.max_orders, 1.0) if self.max_orders > 0 else 1.0

    @property
    def full(self) -> bool:
        return not self.accepting or self.active_orders >= self.max_orders


# Restaurant IDs → their load. Missing IDs (or an empty dict when the lookup
# failed) mean "unknown": the restaurant is ranked without it.
LoadLookup = Callable[[Sequence[str]], Awaitable[dict[str, Load]]]


async def _no_loads(_: Sequence[str]) -> dict[str, Load]:
    return {}


def rating_factor(avg_rating: float | None, review_count: int) -> float:
    """1 − RATING_SHARE … 1, from the Bayesian average of the rating."""
    n = review_count if avg_rating is not None else 0
    mean = (RATING_PRIOR * RATING_PRIOR_WEIGHT + (avg_rating or 0) * n) / (RATING_PRIOR_WEIGHT + n)
    return 1 - RATING_SHARE + RATING_SHARE * mean / 5


def load_factor(load: Load | None) -> float:
    return 1.0 if load is None else 1 - LOAD_SHARE * load.utilization


class Retriever:
    def __init__(
        self, client: AsyncQdrantClient, embedder: Embedder, loads: LoadLookup = _no_loads
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._loads = loads

    async def retrieve(self, query: str, filters: Filters) -> list[Hit]:
        vectors = await self._embedder.query(query)
        if filters.customer_id is not None:
            kind = "order"
            hits = await self._search(
                kind, ORDER_HISTORY, vectors, history_filter(filters.customer_id), filters.limit
            )
        else:
            kind = filters.kind
            query_filter = await self._catalog_filter(filters)
            if query_filter is None:
                hits = []
            elif kind == "cuisine":
                with metrics.QUERY_SECONDS.labels(kind).time():
                    hits = await self._cuisines(vectors, query_filter, filters.limit)
            else:
                hits = await self._search(
                    kind, _COLLECTION[kind], vectors, query_filter, filters.limit
                )
        metrics.SEARCHES.labels(kind, "hits" if hits else "empty").inc()
        return hits

    async def _catalog_filter(self, f: Filters) -> m.Filter | None:
        """`catalog_filter`, with min_rating on dishes resolved to the
        restaurants that qualify. None: no restaurant does, nothing to search."""
        if f.min_rating is None or f.kind == "restaurant":
            return catalog_filter(f)
        rated = await self._rated_restaurants(f)
        return catalog_filter(f, rated) if rated else None

    async def _rated_restaurants(self, f: Filters) -> list[str]:
        """Active restaurants rated at least f.min_rating (and in f's area,
        cuisine, restaurant) — one scroll, IDs only."""
        as_restaurants = Filters(
            kind="restaurant",
            lat=f.lat,
            lng=f.lng,
            radius_km=f.radius_km,
            restaurant_id=f.restaurant_id,
            cuisines=f.cuisines,
            min_rating=f.min_rating,
        )
        points, _ = await self._client.scroll(
            name(RESTAURANTS),
            scroll_filter=catalog_filter(as_restaurants),
            limit=_MAX_RATED_RESTAURANTS,
            with_payload=["restaurant_id"],
            with_vectors=False,
        )
        return [str(p.payload["restaurant_id"]) for p in points if p.payload]

    async def suggest_restaurants(self, query: str, filters: Filters) -> list[Hit]:
        """Restaurants for the query, best first. Each is matched by its best
        dishes under the item filters (price, diet, spice, exclusions,
        cuisine, rating, area), so "mild chicken under Rs 800" suggests only
        places that have one. Score = match × rating factor × load factor:

        - match: the restaurant's best dish score relative to the best
          restaurant's (1.0 for the top one) — rank-based like RRF itself;
        - rating factor: 0.6–1.0 (`rating_factor`);
        - load factor: 0.5–1.0 by how full the kitchen is now; full kitchens
          and ones not taking orders are left out. Unknown load (the lookup
          failed) counts as idle, and the hit says so (`load: null`)."""
        dish_filters = Filters(**{**filters.__dict__, "kind": "item"})
        query_filter = await self._catalog_filter(dish_filters)
        if query_filter is None:
            metrics.SEARCHES.labels("restaurant_suggestion", "empty").inc()
            return []
        vectors = await self._embedder.query(query)
        with metrics.QUERY_SECONDS.labels("restaurant_suggestion").time():
            # Twice the asked-for restaurants: some may be full.
            result = await self._client.query_points_groups(
                name(MENU_ITEMS),
                group_by="restaurant_id",
                prefetch=_prefetch(
                    vectors, query_filter, filters.limit * 2 * _DISHES_PER_RESTAURANT * 4
                ),
                query=_fusion(),
                limit=filters.limit * 2,
                group_size=_DISHES_PER_RESTAURANT,
                with_payload=True,
            )
            groups = {str(g.id): g.hits for g in result.groups if g.hits}
            restaurants = await self._restaurant_payloads(list(groups))
        loads = await self._loads(list(groups))
        best = max((dishes[0].score for dishes in groups.values()), default=0.0) or 1.0
        hits = []
        for rid, dishes in groups.items():
            info = restaurants.get(rid)
            load = loads.get(rid)
            if info is None or (load is not None and load.full):
                continue
            match = dishes[0].score / best
            rating = rating_factor(info.get("avg_rating"), int(info.get("review_count") or 0))
            hits.append(
                Hit(
                    kind="restaurant",
                    id=rid,
                    score=match * rating * load_factor(load),
                    text=str(info.get("text", "")),
                    data={
                        **{k: v for k, v in info.items() if k not in _INTERNAL},
                        "dishes": [
                            {"id": d.id, "score": d.score, **d.data}
                            for d in (_hit("item", p) for p in dishes)
                        ],
                        "match": round(match, 3),
                        "rating_factor": round(rating, 3),
                        "load": None
                        if load is None
                        else {
                            "active_orders": load.active_orders,
                            "max_orders": load.max_orders,
                            "utilization": round(load.utilization, 2),
                            "busy": load.utilization >= BUSY_AT,
                        },
                    },
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        hits = hits[: filters.limit]
        metrics.SEARCHES.labels("restaurant_suggestion", "hits" if hits else "empty").inc()
        return hits

    async def items_by_id(self, item_ids: Sequence[str]) -> list[Hit]:
        """The dishes with these IDs that a customer could order now (the
        same rules as `catalog_filter`), in the order asked — for "my usual":
        dishes from past orders, by ID rather than by search."""
        if not item_ids:
            return []
        points = await self._client.retrieve(
            name(MENU_ITEMS),
            ids=[point_id("menu_item", i) for i in item_ids],
            with_payload=True,
            with_vectors=False,
        )
        orderable = {
            str(p.payload["menu_item_id"]): Hit(
                kind="item",
                id=str(p.payload["menu_item_id"]),
                score=0.0,
                text=str(p.payload.get("text", "")),
                data={k: v for k, v in p.payload.items() if k not in _INTERNAL},
            )
            for p in points
            if p.payload
            and not p.payload.get("deleted")
            and p.payload.get("restaurant_status") == "active"
            and p.payload.get("is_available")
            and p.payload.get("in_stock")
        }
        return [orderable[i] for i in dict.fromkeys(item_ids) if i in orderable]

    async def customer_profile(self, customer_id: str) -> str | None:
        """The customer's order-history summary (app/documents.py
        `customer_summary`), or None if they have no delivered orders."""
        points = await self._client.retrieve(
            name(ORDER_HISTORY),
            ids=[point_id("customer_summary", customer_id)],
            with_payload=["text"],
            with_vectors=False,
        )
        return str(points[0].payload.get("text")) if points and points[0].payload else None

    async def _restaurant_payloads(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        points = await self._client.retrieve(
            name(RESTAURANTS),
            ids=[point_id("restaurant", rid) for rid in ids],
            with_payload=True,
            with_vectors=False,
        )
        return {
            str(p.payload["restaurant_id"]): p.payload
            for p in points
            if p.payload and not p.payload.get("deleted")
        }

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
    from app import qdrant, restaurant_client
    from app.embedding import embedder

    return Retriever(qdrant.client, embedder, restaurant_client.loads)


_default: Retriever | None = None


async def retrieve(query: str, filters: Filters) -> list[Hit]:
    """`default_retriever().retrieve`, created on first use."""
    global _default
    if _default is None:
        _default = default_retriever()
    return await _default.retrieve(query, filters)


async def suggest_restaurants(query: str, filters: Filters) -> list[Hit]:
    """`default_retriever().suggest_restaurants`, created on first use."""
    global _default
    if _default is None:
        _default = default_retriever()
    return await _default.suggest_restaurants(query, filters)
