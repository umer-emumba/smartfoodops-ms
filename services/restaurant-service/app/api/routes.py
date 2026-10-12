from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import TypeAdapter
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from sfo_common.pagination import Page
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, owners, repository
from app.auth_deps import get_current_user_optional, require_roles
from app.db import get_session
from app.models import MenuCategory, MenuItem, Promotion, Restaurant, RestaurantStatusHistory
from app.schemas import (
    MenuCategoryCreate,
    MenuCategoryResponse,
    MenuCategoryUpdate,
    MenuItemCreate,
    MenuItemResponse,
    MenuItemUpdate,
    PromotionCreate,
    PromotionResponse,
    PromotionUpdate,
    RestaurantCreate,
    RestaurantResponse,
    RestaurantUpdate,
    StatusChangeRequest,
    StatusHistoryResponse,
    check_promotion_rules,
)

router = APIRouter(prefix="/v1", tags=["restaurants"])


async def _get_owned_restaurant(
    session: AsyncSession, restaurant_id: str, current: CurrentUser
) -> Restaurant:
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    if restaurant.admin_user_id != current.id and current.role != Role.SYSTEM_ADMIN:
        raise ForbiddenError("Not the owner of this restaurant")
    return restaurant


async def _get_owned_category(
    session: AsyncSession, restaurant_id: str, category_id: str
) -> MenuCategory:
    """Also guards against attaching a menu item to another restaurant's
    category — category_id is client-supplied, so it must be checked, not just
    trusted because the restaurant itself is owned by the caller."""
    category = await repository.get_category(session, category_id)
    if category is None or category.restaurant_id != restaurant_id:
        raise NotFoundError("Menu category not found")
    return category


def _ensure_restaurant_visible(restaurant: Restaurant, current: CurrentUser | None) -> None:
    """A pending/suspended restaurant is only visible to its own admin or a
    system_admin — not the general public, even via a direct ID lookup (see
    docs/architecture.md#restaurant--rider-approval). 404, not 403, so a
    suspended restaurant's existence isn't confirmed to an outsider either."""
    if restaurant.status == "active":
        return
    is_owner = current is not None and current.id == restaurant.admin_user_id
    is_admin = current is not None and current.role == Role.SYSTEM_ADMIN
    if not (is_owner or is_admin):
        raise NotFoundError("Restaurant not found")


_menu_json = TypeAdapter(list[MenuItemResponse])
_categories_json = TypeAdapter(list[MenuCategoryResponse])


async def _public_restaurant_view(
    session: AsyncSession,
    restaurant_id: str,
    current: CurrentUser | None,
    view: cache.View,
    render: Callable[[Restaurant], Awaitable[bytes]],
) -> Response:
    """Serves one of a restaurant's public views through the cache (app/cache.py).

    A hit is served as-is: only active restaurants' views are ever cached, and
    every write invalidates them. On a miss the usual visibility rule applies;
    an active restaurant's view is cached and marked publicly cacheable, while
    a pending/suspended one (visible only to its owner or an admin) is neither
    cached nor shareable.
    """
    cached = await cache.get(restaurant_id, view)
    if cached is not None:
        return cache.json_response(cached, public=True)
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    _ensure_restaurant_visible(restaurant, current)
    body = await render(restaurant)
    if restaurant.status != "active":
        return cache.json_response(body, public=False)
    await cache.put(restaurant_id, view, body)
    return cache.json_response(body, public=True)


def _record_status_change(
    session: AsyncSession,
    restaurant: Restaurant,
    to_status: str,
    current: CurrentUser,
    reason: str | None = None,
) -> None:
    """Changes the status and appends the audit row in the same transaction.
    A no-op transition (e.g. approving an already-active restaurant) records
    nothing, so the trail only ever shows real changes."""
    from_status = restaurant.status
    if from_status == to_status:
        return
    restaurant.status = to_status
    session.add(
        RestaurantStatusHistory(
            restaurant_id=restaurant.id,
            from_status=from_status,
            to_status=to_status,
            actor_id=current.id,
            actor_role=current.role.value,
            reason=reason,
        )
    )


@router.post("/restaurants", response_model=RestaurantResponse, status_code=201)
async def create_restaurant(
    body: RestaurantCreate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Restaurant:
    restaurant = Restaurant(
        admin_user_id=current.id, status="pending_approval", **body.model_dump()
    )
    restaurant = await repository.create_restaurant(session, restaurant)
    session.add(
        RestaurantStatusHistory(
            restaurant_id=restaurant.id,
            from_status=None,
            to_status=restaurant.status,
            actor_id=current.id,
            actor_role=current.role.value,
        )
    )
    await session.commit()
    await owners.record(restaurant.id, restaurant.admin_user_id)
    return restaurant


@router.get("/restaurants", response_model=Page[RestaurantResponse])
async def list_restaurants(
    response: Response,
    city: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Active restaurants only and identical for every caller, so a browser
    # or CDN may reuse it briefly. Not Redis-cached: every city/cursor/limit
    # combination would be a separate entry.
    response.headers["Cache-Control"] = cache.PUBLIC_CACHE_CONTROL
    return await repository.list_restaurants(session, city=city, cursor=cursor, limit=limit)


# Static paths ("mine", "admin") must be registered before the dynamic
# "/{restaurant_id}" route below, or FastAPI would try to match them as an ID.


@router.get("/restaurants/mine", response_model=list[RestaurantResponse])
async def list_my_restaurants(
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[Restaurant]:
    return await repository.list_restaurants_by_owner(session, current.id)


@router.get("/restaurants/admin", response_model=Page[RestaurantResponse])
async def list_restaurants_admin(
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await repository.list_restaurants_admin(
        session, status=status, cursor=cursor, limit=limit
    )


@router.get("/restaurants/{restaurant_id}", response_model=RestaurantResponse)
async def get_restaurant(
    restaurant_id: str,
    current: CurrentUser | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> Response:
    async def render(restaurant: Restaurant) -> bytes:
        return RestaurantResponse.model_validate(restaurant).model_dump_json().encode()

    return await _public_restaurant_view(session, restaurant_id, current, "detail", render)


@router.patch("/restaurants/{restaurant_id}", response_model=RestaurantResponse)
async def update_restaurant(
    restaurant_id: str,
    body: RestaurantUpdate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Restaurant:
    restaurant = await _get_owned_restaurant(session, restaurant_id, current)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(restaurant, field, value)
    await session.commit()
    await cache.invalidate(restaurant_id)
    return restaurant


@router.post("/restaurants/{restaurant_id}/approve", response_model=RestaurantResponse)
async def approve_restaurant(
    restaurant_id: str,
    body: StatusChangeRequest | None = None,
    current: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Restaurant:
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    _record_status_change(session, restaurant, "active", current, body.reason if body else None)
    await session.commit()
    await cache.invalidate(restaurant_id)
    return restaurant


@router.post("/restaurants/{restaurant_id}/suspend", response_model=RestaurantResponse)
async def suspend_restaurant(
    restaurant_id: str,
    body: StatusChangeRequest | None = None,
    current: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Restaurant:
    """Covers both rejecting a pending restaurant and suspending an already-
    active one — both mean 'not allowed to operate', so one action instead of
    two near-duplicate ones."""
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    _record_status_change(session, restaurant, "suspended", current, body.reason if body else None)
    await session.commit()
    await cache.invalidate(restaurant_id)
    return restaurant


@router.get(
    "/restaurants/{restaurant_id}/status-history", response_model=list[StatusHistoryResponse]
)
async def get_status_history(
    restaurant_id: str,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[RestaurantStatusHistory]:
    """Audit trail — visible to the owning restaurant_admin and system_admins."""
    await _get_owned_restaurant(session, restaurant_id, current)
    return await repository.list_status_history(session, restaurant_id)


@router.post(
    "/restaurants/{restaurant_id}/menu/categories",
    response_model=MenuCategoryResponse,
    status_code=201,
)
async def create_category(
    restaurant_id: str,
    body: MenuCategoryCreate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> MenuCategory:
    await _get_owned_restaurant(session, restaurant_id, current)
    category = MenuCategory(restaurant_id=restaurant_id, name=body.name)
    category = await repository.create_category(session, category)
    await session.commit()
    await cache.invalidate(restaurant_id)
    return category


@router.get(
    "/restaurants/{restaurant_id}/menu/categories", response_model=list[MenuCategoryResponse]
)
async def list_categories(
    restaurant_id: str,
    current: CurrentUser | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> Response:
    async def render(_: Restaurant) -> bytes:
        categories = await repository.list_categories(session, restaurant_id)
        return _categories_json.dump_json(
            _categories_json.validate_python(categories, from_attributes=True)
        )

    return await _public_restaurant_view(session, restaurant_id, current, "categories", render)


@router.patch(
    "/restaurants/{restaurant_id}/menu/categories/{category_id}",
    response_model=MenuCategoryResponse,
)
async def update_category(
    restaurant_id: str,
    category_id: str,
    body: MenuCategoryUpdate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> MenuCategory:
    await _get_owned_restaurant(session, restaurant_id, current)
    category = await _get_owned_category(session, restaurant_id, category_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(category, field, value)
    await session.commit()
    await cache.invalidate(restaurant_id)
    return category


@router.delete(
    "/restaurants/{restaurant_id}/menu/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_category(
    restaurant_id: str,
    category_id: str,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _get_owned_restaurant(session, restaurant_id, current)
    category = await _get_owned_category(session, restaurant_id, category_id)
    if await repository.count_items_in_category(session, category_id) > 0:
        raise ConflictError("Category still has menu items — move or delete them first")
    await repository.delete_category(session, category)
    await session.commit()
    await cache.invalidate(restaurant_id)


@router.post(
    "/restaurants/{restaurant_id}/menu/items", response_model=MenuItemResponse, status_code=201
)
async def create_item(
    restaurant_id: str,
    body: MenuItemCreate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> MenuItem:
    await _get_owned_restaurant(session, restaurant_id, current)
    await _get_owned_category(session, restaurant_id, body.category_id)
    item = MenuItem(restaurant_id=restaurant_id, **body.model_dump())
    item = await repository.create_item(session, item)
    await session.commit()
    await cache.invalidate(restaurant_id)
    return item


@router.patch("/restaurants/{restaurant_id}/menu/items/{item_id}", response_model=MenuItemResponse)
async def update_item(
    restaurant_id: str,
    item_id: str,
    body: MenuItemUpdate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> MenuItem:
    await _get_owned_restaurant(session, restaurant_id, current)
    item = await repository.get_item(session, item_id)
    if item is None or item.restaurant_id != restaurant_id:
        raise NotFoundError("Menu item not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    await session.commit()
    await cache.invalidate(restaurant_id)
    return item


@router.delete(
    "/restaurants/{restaurant_id}/menu/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_item(
    restaurant_id: str,
    item_id: str,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _get_owned_restaurant(session, restaurant_id, current)
    item = await repository.get_item(session, item_id)
    if item is None or item.restaurant_id != restaurant_id:
        raise NotFoundError("Menu item not found")
    await repository.delete_item(session, item)
    await session.commit()
    await cache.invalidate(restaurant_id)


@router.get("/restaurants/{restaurant_id}/menu", response_model=list[MenuItemResponse])
async def get_menu(
    restaurant_id: str,
    current: CurrentUser | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> Response:
    async def render(_: Restaurant) -> bytes:
        items = await repository.list_menu(session, restaurant_id)
        return _menu_json.dump_json(_menu_json.validate_python(items, from_attributes=True))

    return await _public_restaurant_view(session, restaurant_id, current, "menu", render)


@router.post(
    "/restaurants/{restaurant_id}/promotions", response_model=PromotionResponse, status_code=201
)
async def create_promotion(
    restaurant_id: str,
    body: PromotionCreate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Promotion:
    await _get_owned_restaurant(session, restaurant_id, current)
    if await repository.get_promotion_by_code(session, restaurant_id, body.code) is not None:
        raise ConflictError("A promotion with this code already exists for this restaurant")
    promotion = Promotion(restaurant_id=restaurant_id, **body.model_dump())
    try:
        promotion = await repository.create_promotion(session, promotion)
        await session.commit()
    except IntegrityError as exc:
        # Two concurrent creates with the same code both passed the pre-check.
        await session.rollback()
        raise ConflictError(
            "A promotion with this code already exists for this restaurant"
        ) from exc
    return promotion


@router.patch(
    "/restaurants/{restaurant_id}/promotions/{promotion_id}", response_model=PromotionResponse
)
async def update_promotion(
    restaurant_id: str,
    promotion_id: str,
    body: PromotionUpdate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Promotion:
    await _get_owned_restaurant(session, restaurant_id, current)
    promotion = await repository.get_promotion(session, promotion_id)
    if promotion is None or promotion.restaurant_id != restaurant_id:
        raise NotFoundError("Promotion not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(promotion, field, value)
    # Re-checked on the merged row: a PATCH of only valid_until (or only
    # discount_value) must still be consistent with the stored fields.
    try:
        check_promotion_rules(
            promotion.discount_type,
            promotion.discount_value,
            promotion.valid_from,
            promotion.valid_until,
        )
    except ValueError as exc:
        await session.rollback()
        raise ValidationAppError(str(exc)) from exc
    await session.commit()
    return promotion


@router.delete(
    "/restaurants/{restaurant_id}/promotions/{promotion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_promotion(
    restaurant_id: str,
    promotion_id: str,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _get_owned_restaurant(session, restaurant_id, current)
    promotion = await repository.get_promotion(session, promotion_id)
    if promotion is None or promotion.restaurant_id != restaurant_id:
        raise NotFoundError("Promotion not found")
    await repository.delete_promotion(session, promotion)
    await session.commit()


@router.get("/restaurants/{restaurant_id}/promotions", response_model=list[PromotionResponse])
async def list_promotions(
    restaurant_id: str,
    current: CurrentUser | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> list[Promotion]:
    """Public callers see only promotions that are active and currently within
    their validity window. The owner and system_admins see all of them,
    including expired and scheduled ones, so they can manage them."""
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    _ensure_restaurant_visible(restaurant, current)
    is_manager = current is not None and (
        current.id == restaurant.admin_user_id or current.role == Role.SYSTEM_ADMIN
    )
    if is_manager:
        return await repository.list_all_promotions(session, restaurant_id)
    return await repository.list_active_promotions(session, restaurant_id)
