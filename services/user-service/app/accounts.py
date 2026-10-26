from sfo_common.errors import ConflictError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.models import User


async def create_user_or_conflict(session: AsyncSession, user: User) -> User:
    """Shared by self-registration (app/api/routes.py) and the first-admin
    command (app/create_admin.py). Pre-checks give a specific message; the
    IntegrityError catch covers the race where two signups with the same
    email/phone pass the pre-check concurrently — a 409 either way, never a
    raw 500."""
    if await repository.get_user_by_email(session, user.email) is not None:
        raise ConflictError("Email is already registered")
    if await repository.get_user_by_phone(session, user.phone_number) is not None:
        raise ConflictError("Phone number is already registered")
    try:
        user = await repository.create_user(session, user=user)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("Email or phone number is already registered") from exc
    return user
