from fastapi import APIRouter, Depends, Query, status
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth_deps import get_current_user_optional, require_roles
from app.db import get_session
from app.models import MenuCategory, MenuItem, Promotion, Restaurant
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


@router.post("/restaurants", response_model=RestaurantResponse, status_code=201)
async def create_restaurant(
    body: RestaurantCreate,
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Restaurant:
    restaurant = Restaurant(admin_user_id=current.id, **body.model_dump())
    restaurant = await repository.create_restaurant(session, restaurant)
    await session.commit()
    return restaurant


@router.get("/restaurants", response_model=list[RestaurantResponse])
async def list_restaurants(
    city: str | None = Query(default=None),
    limit: int = Query(default=20, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[Restaurant]:
    return await repository.list_restaurants(session, city=city, limit=limit)


# Static paths ("mine", "admin") must be registered before the dynamic
# "/{restaurant_id}" route below, or FastAPI would try to match them as an ID.


@router.get("/restaurants/mine", response_model=list[RestaurantResponse])
async def list_my_restaurants(
    current: CurrentUser = Depends(require_roles(Role.RESTAURANT_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[Restaurant]:
    return await repository.list_restaurants_by_owner(session, current.id)


@router.get("/restaurants/admin", response_model=list[RestaurantResponse])
async def list_restaurants_admin(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[Restaurant]:
    return await repository.list_restaurants_admin(session, status=status, limit=limit)


@router.get("/restaurants/{restaurant_id}", response_model=RestaurantResponse)
async def get_restaurant(
    restaurant_id: str,
    current: CurrentUser | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> Restaurant:
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    _ensure_restaurant_visible(restaurant, current)
    return restaurant


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
    return restaurant


@router.post("/restaurants/{restaurant_id}/approve", response_model=RestaurantResponse)
async def approve_restaurant(
    restaurant_id: str,
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Restaurant:
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    restaurant.status = "active"
    await session.commit()
    return restaurant


@router.post("/restaurants/{restaurant_id}/suspend", response_model=RestaurantResponse)
async def suspend_restaurant(
    restaurant_id: str,
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> Restaurant:
    """Covers both rejecting a pending restaurant and suspending an already-
    active one — both mean 'not allowed to operate', so one action instead of
    two near-duplicate ones."""
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    restaurant.status = "suspended"
    await session.commit()
    return restaurant


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
    return category


@router.get(
    "/restaurants/{restaurant_id}/menu/categories", response_model=list[MenuCategoryResponse]
)
async def list_categories(
    restaurant_id: str,
    current: CurrentUser | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> list[MenuCategory]:
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    _ensure_restaurant_visible(restaurant, current)
    return await repository.list_categories(session, restaurant_id)


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


@router.post("/restaurants/{restaurant_id}/menu/items", response_model=MenuItemResponse, status_code=201)
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


@router.get("/restaurants/{restaurant_id}/menu", response_model=list[MenuItemResponse])
async def get_menu(
    restaurant_id: str,
    current: CurrentUser | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> list[MenuItem]:
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    _ensure_restaurant_visible(restaurant, current)
    return await repository.list_menu(session, restaurant_id)


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
    promotion = await repository.create_promotion(session, promotion)
    await session.commit()
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
    if promotion.valid_until <= promotion.valid_from:
        raise ValidationAppError("valid_until must be after valid_from")
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
    restaurant = await repository.get_restaurant(session, restaurant_id)
    if restaurant is None:
        raise NotFoundError("Restaurant not found")
    _ensure_restaurant_visible(restaurant, current)
    return await repository.list_active_promotions(session, restaurant_id)
