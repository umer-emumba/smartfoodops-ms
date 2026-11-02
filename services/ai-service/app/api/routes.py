from collections.abc import Awaitable

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
    limit: int = LIMIT,
) -> dict:
    """Semantic + keyword search over the catalog (docs/week4-plan.md step 4).
    Public, like the menus it searches: only active restaurants and items
    that can be ordered right now."""
    if (lat is None) != (lng is None):
        raise ValidationAppError("lat and lng go together")
    if kind not in ("item", "cuisine") and (max_price is not None or dietary):
        raise ValidationAppError("max_price and dietary apply to kind=item or kind=cuisine")
    filters = Filters(
        kind=kind,
        lat=lat,
        lng=lng,
        radius_km=radius_km if lat is not None else None,
        max_price=max_price,
        dietary=tuple(dietary),
        restaurant_id=restaurant_id,
        limit=limit,
    )
    hits = await _run(retrieval.retrieve(q, filters))
    return {"query": q, "kind": kind, "hits": hits}


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
