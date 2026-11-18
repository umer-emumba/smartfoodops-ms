from collections.abc import Awaitable, Sequence

from fastapi import APIRouter, Depends, Query
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ServiceUnavailableError, ValidationAppError

from app import retrieval
from app.auth_deps import require_roles
from app.retrieval import Filters, Hit, Kind
from app.schemas import DietaryTag, SearchResponse

router = APIRouter(prefix="/v1/ai", tags=["ai"])

Q = Query(min_length=1, max_length=300, description="What to look for, in plain words")
LIMIT = Query(default=10, ge=1, le=50)
MAX_SPICE = Query(default=None, ge=0, le=3, description="0 not spicy … 3 very spicy")
CUISINE = Query(
    default=[], max_length=5, description='Any of these words in the cuisine ("thai", "bbq")'
)
EXCLUDE = Query(
    default=[], max_length=10, description='No dish mentioning these words ("beef", "egg")'
)
MIN_RATING = Query(default=None, ge=1, le=5, description="The restaurant's average rating")
_WORD_MAX = 40


async def _run(search: Awaitable[list[Hit]]) -> list[Hit]:
    """Qdrant down → 503. A collection that doesn't exist yet (index never
    built, `python -m app.reindex` not run) is no results, not an error."""
    try:
        return await search
    except ResponseHandlingException as exc:
        raise ServiceUnavailableError("Search is unavailable") from exc
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return []
        raise ServiceUnavailableError("Search is unavailable") from exc


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Q,
    kind: Kind = Query(default="item"),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_km: float = Query(default=5.0, gt=0, le=50, description="Used with lat/lng"),
    max_price: float | None = Query(default=None, ge=0, description="PKR; items and cuisines"),
    dietary: list[DietaryTag] = Query(default=[], description="All must apply; items, cuisines"),
    restaurant_id: str | None = Query(default=None, max_length=64),
    max_spice: int | None = MAX_SPICE,
    cuisine: list[str] = CUISINE,
    exclude: list[str] = EXCLUDE,
    min_rating: float | None = MIN_RATING,
    limit: int = LIMIT,
) -> dict:
    """Semantic + keyword search over the catalog (docs/week4-plan.md step 4,
    week5-plan step 1). Public, like the menus it searches: only active
    restaurants and items that can be ordered right now."""
    dish_only = max_price is not None or dietary or max_spice is not None or exclude
    if kind not in ("item", "cuisine") and dish_only:
        raise ValidationAppError(
            "max_price, dietary, max_spice and exclude apply to kind=item or kind=cuisine"
        )
    if kind == "review" and (cuisine or min_rating is not None):
        raise ValidationAppError("cuisine and min_rating don't apply to kind=review")
    filters = _filters(
        kind=kind,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        max_price=max_price,
        dietary=dietary,
        restaurant_id=restaurant_id,
        max_spice=max_spice,
        cuisine=cuisine,
        exclude=exclude,
        min_rating=min_rating,
        limit=limit,
    )
    hits = await _run(retrieval.retrieve(q, filters))
    return {"query": q, "kind": kind, "hits": hits}


@router.get("/restaurants/suggest", response_model=SearchResponse)
async def suggest_restaurants(
    q: str = Q,
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_km: float = Query(default=5.0, gt=0, le=50, description="Used with lat/lng"),
    max_price: float | None = Query(default=None, ge=0, description="PKR, per dish"),
    dietary: list[DietaryTag] = Query(default=[], description="All must apply to a dish"),
    max_spice: int | None = MAX_SPICE,
    cuisine: list[str] = CUISINE,
    exclude: list[str] = EXCLUDE,
    min_rating: float | None = MIN_RATING,
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    """Restaurants for what the customer wants (docs/week5-plan.md step 1):
    matched by their best dishes under the dish filters, then weighed by
    rating and how busy the kitchen is right now. Each hit carries its
    matching `dishes` and the parts of its score (`match`, `rating_factor`,
    `load`), so the assistant can say why. Full kitchens are left out."""
    filters = _filters(
        kind="item",
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        max_price=max_price,
        dietary=dietary,
        restaurant_id=None,
        max_spice=max_spice,
        cuisine=cuisine,
        exclude=exclude,
        min_rating=min_rating,
        limit=limit,
    )
    hits = await _run(retrieval.suggest_restaurants(q, filters))
    return {"query": q, "kind": "restaurant", "hits": hits}


def _filters(
    *,
    kind: Kind,
    lat: float | None,
    lng: float | None,
    radius_km: float,
    max_price: float | None,
    dietary: Sequence[str],
    restaurant_id: str | None,
    max_spice: int | None,
    cuisine: list[str],
    exclude: list[str],
    min_rating: float | None,
    limit: int,
) -> Filters:
    if (lat is None) != (lng is None):
        raise ValidationAppError("lat and lng go together")
    for word in (*cuisine, *exclude):
        if not word.strip() or len(word) > _WORD_MAX:
            raise ValidationAppError(f"cuisine and exclude words: 1–{_WORD_MAX} characters")
    return Filters(
        kind=kind,
        lat=lat,
        lng=lng,
        radius_km=radius_km if lat is not None else None,
        max_price=max_price,
        dietary=tuple(dietary),
        restaurant_id=restaurant_id,
        max_spice=max_spice,
        cuisines=tuple(cuisine),
        exclude=tuple(exclude),
        min_rating=min_rating,
        limit=limit,
    )


@router.get("/search/history", response_model=SearchResponse)
async def search_history(
    q: str = Q,
    limit: int = LIMIT,
    current: CurrentUser = Depends(require_roles(Role.CUSTOMER)),
) -> dict:
    """The caller's own delivered orders, by what was in them ("that spicy
    chicken from last month"). Always filtered to the caller."""
    hits = await _run(retrieval.retrieve(q, Filters(customer_id=current.id, limit=limit)))
    return {"query": q, "kind": "order", "hits": hits}
