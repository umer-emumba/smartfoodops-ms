from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MenuCategory, MenuItem, Promotion, Restaurant


async def create_restaurant(session: AsyncSession, restaurant: Restaurant) -> Restaurant:
    session.add(restaurant)
    await session.flush()
    return restaurant


async def get_restaurant(session: AsyncSession, restaurant_id: str) -> Restaurant | None:
    return await session.get(Restaurant, restaurant_id)


async def list_restaurants(
    session: AsyncSession, *, city: str | None, limit: int
) -> list[Restaurant]:
    stmt = select(Restaurant).where(Restaurant.status == "active").order_by(Restaurant.created_at.desc())
    if city:
        stmt = stmt.where(Restaurant.city == city)
    result = await session.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def list_restaurants_by_owner(session: AsyncSession, admin_user_id: str) -> list[Restaurant]:
    """Regardless of status — an owner needs to see their own pending/suspended
    restaurants too, not just active ones."""
    result = await session.execute(
        select(Restaurant)
        .where(Restaurant.admin_user_id == admin_user_id)
        .order_by(Restaurant.created_at.desc())
    )
    return list(result.scalars().all())


async def list_restaurants_admin(
    session: AsyncSession, *, status: str | None, limit: int
) -> list[Restaurant]:
    """system_admin management view — unlike list_restaurants, not filtered to
    'active' by default, so pending/suspended restaurants can be reviewed."""
    stmt = select(Restaurant).order_by(Restaurant.created_at.desc())
    if status:
        stmt = stmt.where(Restaurant.status == status)
    result = await session.execute(stmt.limit(limit))
    return list(result.scalars().all())


async def create_category(session: AsyncSession, category: MenuCategory) -> MenuCategory:
    session.add(category)
    await session.flush()
    return category


async def get_category(session: AsyncSession, category_id: str) -> MenuCategory | None:
    return await session.get(MenuCategory, category_id)


async def list_categories(session: AsyncSession, restaurant_id: str) -> list[MenuCategory]:
    result = await session.execute(
        select(MenuCategory)
        .where(MenuCategory.restaurant_id == restaurant_id)
        .order_by(MenuCategory.name)
    )
    return list(result.scalars().all())


async def count_items_in_category(session: AsyncSession, category_id: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(MenuItem).where(MenuItem.category_id == category_id)
    )
    return result.scalar_one()


async def delete_category(session: AsyncSession, category: MenuCategory) -> None:
    await session.delete(category)


async def create_item(session: AsyncSession, item: MenuItem) -> MenuItem:
    session.add(item)
    await session.flush()
    return item


async def get_item(session: AsyncSession, item_id: str) -> MenuItem | None:
    return await session.get(MenuItem, item_id)


async def delete_item(session: AsyncSession, item: MenuItem) -> None:
    await session.delete(item)


async def list_menu(session: AsyncSession, restaurant_id: str) -> list[MenuItem]:
    result = await session.execute(
        select(MenuItem).where(MenuItem.restaurant_id == restaurant_id).order_by(MenuItem.name)
    )
    return list(result.scalars().all())


async def create_promotion(session: AsyncSession, promotion: Promotion) -> Promotion:
    session.add(promotion)
    await session.flush()
    return promotion


async def get_promotion(session: AsyncSession, promotion_id: str) -> Promotion | None:
    return await session.get(Promotion, promotion_id)


async def get_promotion_by_code(
    session: AsyncSession, restaurant_id: str, code: str
) -> Promotion | None:
    result = await session.execute(
        select(Promotion).where(Promotion.restaurant_id == restaurant_id, Promotion.code == code)
    )
    return result.scalar_one_or_none()


async def delete_promotion(session: AsyncSession, promotion: Promotion) -> None:
    await session.delete(promotion)


async def list_active_promotions(session: AsyncSession, restaurant_id: str) -> list[Promotion]:
    result = await session.execute(
        select(Promotion).where(Promotion.restaurant_id == restaurant_id, Promotion.is_active)
    )
    return list(result.scalars().all())
