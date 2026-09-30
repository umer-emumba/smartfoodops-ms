import hmac
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, status
from sfo_auth import (
    CurrentUser,
    Role,
    create_access_token,
    generate_refresh_token,
    hash_secret,
    parse_refresh_token,
    verify_secret,
)
from sfo_auth.denylist import block_user, unblock_user
from sfo_common.errors import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth_deps import get_current_user, require_roles
from app.config import settings
from app.db import get_session
from app.models import Address, RefreshToken, User
from app.redis_client import redis
from app.schemas import (
    PUBLIC_ROLES,
    AddressCreate,
    AddressResponse,
    BootstrapAdminRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)

router = APIRouter(prefix="/v1", tags=["auth"])

# Verified against when the email doesn't exist, so an unknown email costs the
# same argon2 work as a wrong password — response time doesn't reveal which
# emails have accounts.
_DUMMY_PASSWORD_HASH = hash_secret("timing-equalizer-not-a-real-password")


async def _create_user_or_conflict(session: AsyncSession, user: User) -> User:
    """Pre-checks give a specific message; the IntegrityError catch covers the
    race where two signups with the same email/phone pass the pre-check
    concurrently — a 409 either way, never a raw 500."""
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


def _issue_tokens(user: User) -> tuple[str, RefreshToken, str]:
    """Returns (access_token, refresh_token_row_to_persist, full_refresh_token_string)."""
    access_token = create_access_token(
        user_id=user.id,
        role=Role(user.role),
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=settings.access_token_ttl_minutes,
    )
    lookup_id, raw_secret, token_str = generate_refresh_token()
    refresh_row = RefreshToken(
        id=lookup_id,
        user_id=user.id,
        token_hash=hash_secret(raw_secret),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
    )
    return access_token, refresh_row, token_str


@router.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> User:
    if body.role not in PUBLIC_ROLES:
        raise ForbiddenError(f"Cannot self-register with role '{body.role}'")
    user = User(
        email=body.email,
        hashed_password=hash_secret(body.password),
        role=body.role,
        full_name=body.full_name,
        phone_number=body.phone_number,
    )
    return await _create_user_or_conflict(session, user)


@router.post(
    "/auth/bootstrap-admin", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
async def bootstrap_admin(
    body: BootstrapAdminRequest,
    session: AsyncSession = Depends(get_session),
    x_bootstrap_secret: str = Header(...),
) -> User:
    """The only way a system_admin account is created — self-registration is
    limited to PUBLIC_ROLES (see docs/architecture.md#restaurant--rider-approval). Gated by a
    shared secret rather than open registration, since this role can block
    any user and approve/suspend any restaurant or rider.

    One-shot: once any system_admin exists this endpoint refuses (409), so a
    leaked secret can't be used to mint further admins later. Further admins
    are an explicit follow-up (an admin-only "promote user" action), not this
    endpoint."""
    # Compared as bytes — compare_digest raises TypeError on non-ASCII str.
    if not hmac.compare_digest(
        x_bootstrap_secret.encode(), settings.admin_bootstrap_secret.encode()
    ):
        raise UnauthorizedError("Invalid bootstrap secret")
    if await repository.any_user_with_role(session, Role.SYSTEM_ADMIN.value):
        raise ConflictError("A system_admin already exists — bootstrap is disabled")
    user = User(
        email=body.email,
        hashed_password=hash_secret(body.password),
        role=Role.SYSTEM_ADMIN,
        full_name=body.full_name,
        phone_number=body.phone_number,
    )
    return await _create_user_or_conflict(session, user)


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await repository.get_user_by_email(session, body.email)
    if user is None:
        verify_secret(body.password, _DUMMY_PASSWORD_HASH)
        raise UnauthorizedError("Invalid email or password")
    if not verify_secret(body.password, user.hashed_password):
        raise UnauthorizedError("Invalid email or password")
    if user.is_blocked:
        raise ForbiddenError("Account is blocked")

    access_token, refresh_row, refresh_token_str = _issue_tokens(user)
    await repository.create_refresh_token(session, token=refresh_row)
    await session.commit()
    return TokenResponse(access_token=access_token, refresh_token=refresh_token_str)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    try:
        lookup_id, raw_secret = parse_refresh_token(body.refresh_token)
    except ValueError as exc:
        raise UnauthorizedError("Invalid refresh token") from exc

    record = await repository.get_refresh_token(session, lookup_id)
    now = datetime.now(UTC)
    if record is None or not verify_secret(raw_secret, record.token_hash):
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
    if record is None or not verify_secret(raw_secret, record.token_hash):
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


@router.post("/users/{user_id}/block", response_model=UserResponse)
async def block(
    user_id: str,
    _: CurrentUser = Depends(require_roles(Role.SYSTEM_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await repository.get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found")
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
