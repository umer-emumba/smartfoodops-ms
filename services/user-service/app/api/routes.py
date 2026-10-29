from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, status
from sfo_auth import (
    CurrentUser,
    Role,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_password_blocking,
    hash_refresh_secret,
    parse_refresh_token,
    verify_password,
    verify_refresh_secret,
)
from sfo_auth.denylist import block_user, unblock_user
from sfo_common.errors import (
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationAppError,
)
from sfo_common.pagination import Page
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.accounts import create_user_or_conflict
from app.auth_deps import get_current_user, require_roles
from app.config import settings
from app.db import get_session
from app.models import Address, RefreshToken, User
from app.redis_client import redis
from app.schemas import (
    AddressCreate,
    AddressResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.signing import get_signer

router = APIRouter(prefix="/v1", tags=["auth"])

# Verified against when the email doesn't exist, so an unknown email costs the
# same argon2 work as a wrong password — response time doesn't reveal which
# emails have accounts.
_DUMMY_PASSWORD_HASH = hash_password_blocking("timing-equalizer-not-a-real-password")


def _issue_tokens(user: User) -> tuple[str, RefreshToken, str]:
    """Returns (access_token, refresh_token_row_to_persist, full_refresh_token_string)."""
    access_token = create_access_token(
        user_id=user.id,
        role=Role(user.role),
        signer=get_signer(),
        ttl_minutes=settings.access_token_ttl_minutes,
    )
    lookup_id, raw_secret, token_str = generate_refresh_token()
    refresh_row = RefreshToken(
        id=lookup_id,
        user_id=user.id,
        token_hash=hash_refresh_secret(raw_secret),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
    )
    return access_token, refresh_row, token_str


@router.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> User:
    # body.role is already limited to PUBLIC_ROLES by the schema (422 otherwise).
    user = User(
        email=body.email,
        hashed_password=await hash_password(body.password),
        role=body.role,
        full_name=body.full_name,
        phone_number=body.phone_number,
    )
    return await create_user_or_conflict(session, user)


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await repository.get_user_by_email(session, body.email)
    if user is None:
        await verify_password(body.password, _DUMMY_PASSWORD_HASH)
        raise UnauthorizedError("Invalid email or password")
    if not await verify_password(body.password, user.hashed_password):
        raise UnauthorizedError("Invalid email or password")
    if user.is_blocked:
        raise ForbiddenError("Account is blocked")

    access_token, refresh_row, refresh_token_str = _issue_tokens(user)
    await repository.create_refresh_token(session, token=refresh_row)
    await session.commit()
    return TokenResponse(access_token=access_token, refresh_token=refresh_token_str)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    try:
        lookup_id, raw_secret = parse_refresh_token(body.refresh_token)
    except ValueError as exc:
        raise UnauthorizedError("Invalid refresh token") from exc

    record = await repository.get_refresh_token(session, lookup_id)
    now = datetime.now(UTC)
    if record is None or not verify_refresh_secret(raw_secret, record.token_hash):
        raise UnauthorizedError("Invalid or expired refresh token")
    if record.revoked_at is not None:
        # A genuine, already-rotated token being presented again means two
        # parties hold it — one of them stole it. We can't tell which, so end
        # every session for this user; the real owner just logs in again.
        await repository.revoke_all_refresh_tokens(session, record.user_id, at=now)
        await session.commit()
        raise UnauthorizedError("Refresh token reuse detected — all sessions revoked")
    if record.expires_at < now:
        raise UnauthorizedError("Invalid or expired refresh token")

    # Authoritative block check — not subject to the deny-list's TTL, see
    # docs/technical-documentation.md, "Auth: 15-minute access tokens...".
    user = await repository.get_user_by_id(session, record.user_id)
    if user is None or user.is_blocked:
        raise ForbiddenError("Account is blocked")

    # Rotation: revoke the presented token, issue a fresh pair. Losing the
    # compare-and-set means a concurrent request already rotated this token.
    if not await repository.revoke_refresh_token_if_active(session, record.id, at=now):
        await session.rollback()
        raise UnauthorizedError("Invalid or expired refresh token")
    access_token, new_refresh_row, refresh_token_str = _issue_tokens(user)
    await repository.create_refresh_token(session, token=new_refresh_row)
    await session.commit()
    return TokenResponse(access_token=access_token, refresh_token=refresh_token_str)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, session: AsyncSession = Depends(get_session)) -> None:
    try:
        lookup_id, raw_secret = parse_refresh_token(body.refresh_token)
    except ValueError:
        return  # already "logged out" as far as the client should know
    record = await repository.get_refresh_token(session, lookup_id)
    # The secret must match — knowing a lookup_id alone (e.g. from a log line)
    # must not let someone end another user's session.
    if record is None or not verify_refresh_secret(raw_secret, record.token_hash):
        return
    if await repository.revoke_refresh_token_if_active(session, record.id, at=datetime.now(UTC)):
        await session.commit()


@router.get("/users/me", response_model=UserResponse)
async def get_me(
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await repository.get_user_by_id(session, current.id)
    if user is None:
        raise NotFoundError("User not found")
    return user


# Registered after /users/me, so "me" is never captured as a {user_id}.
@router.get("/users", response_model=Page[UserResponse])
async def list_users(
    role: Role | None = Query(default=None),
    is_blocked: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """system_admin user management: find users to review or block, by role,
    blocked state, or a search over email/name/phone. Newest first; pass the
    previous page's `next_cursor` to get the next page."""
    # Checked after stripping, so "  " can't pass as a 2-character search.
    if q is not None:
        q = q.strip()
        if len(q) < 2:
            raise ValidationAppError("Search term must be at least 2 characters")
    return await repository.list_users(
        session,
        role=role.value if role else None,
        is_blocked=is_blocked,
        q=q,
        cursor=cursor,
        limit=limit,
    )


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await repository.get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return user


@router.post("/users/{user_id}/block", response_model=UserResponse)
async def block(
    user_id: str,
    current: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await repository.get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found")
    # An admin blocking themselves would lock the platform out of its only
    # admin (app/create_admin.py is one-shot). Blocking other admins is refused
    # too: admin-vs-admin disputes need an out-of-band process, not an
    # endpoint any single admin can use against the others.
    if user.id == current.id:
        raise ForbiddenError("You can't block your own account")
    if user.role == Role.SYSTEM_ADMIN.value:
        raise ForbiddenError("system_admin accounts can't be blocked through the API")
    # Deny-list first, then the DB: if Redis is down this fails before anything
    # is committed, instead of committing a block that existing access tokens
    # would ignore for up to their full lifetime. (A DB failure after the
    # Redis write just denies the user until the TTL — the safe direction.)
    await block_user(redis, user_id, ttl_seconds=settings.access_token_ttl_minutes * 60)
    user.is_blocked = True
    await session.commit()
    return user


@router.post("/users/{user_id}/unblock", response_model=UserResponse)
async def unblock(
    user_id: str,
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await repository.get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found")
    user.is_blocked = False
    await session.commit()
    # Removes the user from the Redis deny-list, so the gateway and the services
    # accept their tokens again.
    await unblock_user(redis, user_id)
    return user


@router.get("/users/me/addresses", response_model=list[AddressResponse])
async def list_my_addresses(
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Address]:
    return await repository.list_addresses(session, current.id)


@router.post(
    "/users/me/addresses", response_model=AddressResponse, status_code=status.HTTP_201_CREATED
)
async def add_my_address(
    body: AddressCreate,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Address:
    address = Address(user_id=current.id, **body.model_dump())
    address = await repository.create_address(session, address)
    await session.commit()
    return address
