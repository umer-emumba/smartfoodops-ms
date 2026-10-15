"""The inbox API against the real notification database."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.config import settings
from app.main import app
from app.models import Notification
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role, create_access_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db

Factory = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _as(user_id: str) -> dict[str, str]:
    token = create_access_token(
        user_id=user_id,
        role=Role.CUSTOMER,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=5,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _add(factory: Factory, user_id: str, channel: str, minute: int) -> str:
    notification = Notification(
        user_id=user_id,
        recipient_role="customer",
        event_id=f"e-{minute}",
        type="order_update",
        channel=channel,
        content=f"message {minute}",
        created_at=T0 + timedelta(minutes=minute),
    )
    async with factory() as session:
        session.add(notification)
        await session.commit()
    return notification.id


async def test_inbox_lists_in_app_only_newest_first_with_unread_filter(
    session_factory: Factory, client: AsyncClient
) -> None:
    first = await _add(session_factory, "u-1", "in_app", 1)
    second = await _add(session_factory, "u-1", "in_app", 2)
    await _add(session_factory, "u-1", "email", 2)
    await _add(session_factory, "u-2", "in_app", 3)
    me = _as("u-1")

    inbox = (await client.get("/v1/notifications", headers=me)).json()["items"]
    assert [n["id"] for n in inbox] == [second, first]

    read = await client.post(f"/v1/notifications/{second}/read", headers=me)
    assert read.status_code == 200 and read.json()["read_at"] is not None
    unread = (await client.get("/v1/notifications?unread=true", headers=me)).json()["items"]
    assert [n["id"] for n in unread] == [first]


async def test_mark_read_is_idempotent_and_private(
    session_factory: Factory, client: AsyncClient
) -> None:
    mine = await _add(session_factory, "u-1", "in_app", 1)
    email = await _add(session_factory, "u-1", "email", 1)
    me, other = _as("u-1"), _as("u-2")
    first = (await client.post(f"/v1/notifications/{mine}/read", headers=me)).json()
    again = (await client.post(f"/v1/notifications/{mine}/read", headers=me)).json()
    assert first["read_at"] == again["read_at"]
    assert (await client.post(f"/v1/notifications/{mine}/read", headers=other)).status_code == 404
    assert (await client.post(f"/v1/notifications/{email}/read", headers=me)).status_code == 404
