from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Address, RefreshToken, User


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def create_user(session: AsyncSession, *, user: User) -> User:
    session.add(user)
    await session.flush()
    return user


async def create_refresh_token(session: AsyncSession, *, token: RefreshToken) -> RefreshToken:
    session.add(token)
    await session.flush()
    return token


async def get_refresh_token(session: AsyncSession, lookup_id: str) -> RefreshToken | None:
    return await session.get(RefreshToken, lookup_id)


async def revoke_refresh_token(session: AsyncSession, token: RefreshToken, *, at: datetime) -> None:
    token.revoked_at = at
    await session.flush()


async def list_addresses(session: AsyncSession, user_id: str) -> list[Address]:
    result = await session.execute(
        select(Address).where(Address.user_id == user_id).order_by(Address.created_at.desc())
    )
    return list(result.scalars().all())


async def create_address(session: AsyncSession, address: Address) -> Address:
    if address.is_default:
        result = await session.execute(
            select(Address).where(Address.user_id == address.user_id, Address.is_default)
        )
        for existing in result.scalars().all():
            existing.is_default = False
    session.add(address)
    await session.flush()
    return address
