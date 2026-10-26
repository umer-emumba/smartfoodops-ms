import asyncio
import io
import sys

import pytest
from app import create_admin as command
from app.create_admin import AdminExistsError, create_admin
from app.models import User
from app.schemas import NewAdmin
from sfo_auth import Role, verify_password
from sfo_common.errors import ConflictError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _run_main(monkeypatch: pytest.MonkeyPatch, *args: str, stdin: str = "") -> int:
    monkeypatch.setattr(sys, "argv", ["create_admin", *args])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return command.main()


def _args(email: str = "admin@example.com", phone: str = "+923001234567") -> list[str]:
    return ["--email", email, "--full-name", "Admin", "--phone", phone, "--password-stdin"]


@pytest.mark.parametrize(
    ("args", "stdin", "field"),
    [
        (_args(), "short\n", "password"),
        (_args(email="not-an-email"), "LongEnough123!\n", "email"),
        (_args(phone="12345"), "LongEnough123!\n", "phone_number"),
    ],
)
def test_invalid_input_is_rejected_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    stdin: str,
    field: str,
) -> None:
    async def must_not_run(_: NewAdmin) -> int:
        raise AssertionError("reached the database step")

    monkeypatch.setattr(command, "_run", must_not_run)
    assert _run_main(monkeypatch, *args, stdin=stdin) == 1
    assert capsys.readouterr().err.startswith(field)


def test_password_from_stdin_keeps_spaces_drops_only_the_line_ending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[NewAdmin] = []

    async def capture(new: NewAdmin) -> int:
        seen.append(new)
        return 0

    monkeypatch.setattr(command, "_run", capture)
    assert _run_main(monkeypatch, *_args(email="A@Example.com"), stdin=" pass word \n") == 0
    assert seen[0].password == " pass word "
    assert seen[0].email == "a@example.com"  # normalised like self-registration


def _new(email: str = "admin@example.com", phone: str = "+923001234567") -> NewAdmin:
    return NewAdmin(email=email, password="LongEnough123!", full_name="Admin", phone_number=phone)


async def _admins(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(User).where(User.role == Role.SYSTEM_ADMIN)
            )
        ).scalar_one()


@pytest.fixture
def use_test_db(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    monkeypatch.setattr(command, "SessionFactory", session_factory)
    return session_factory


@pytest.mark.db
async def test_creates_the_first_admin_with_a_hashed_password(
    use_test_db: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_admin(_new())
    assert user.role == Role.SYSTEM_ADMIN
    assert user.hashed_password != "LongEnough123!"
    assert await verify_password("LongEnough123!", user.hashed_password)
    assert await _admins(use_test_db) == 1


@pytest.mark.db
async def test_refuses_once_an_admin_exists(
    use_test_db: async_sessionmaker[AsyncSession],
) -> None:
    await create_admin(_new())
    with pytest.raises(AdminExistsError):
        await create_admin(_new(email="second@example.com", phone="+923001234568"))
    assert await _admins(use_test_db) == 1


@pytest.mark.db
async def test_two_runs_at_once_create_only_one_admin(
    use_test_db: async_sessionmaker[AsyncSession],
) -> None:
    results = await asyncio.gather(
        create_admin(_new()),
        create_admin(_new(email="second@example.com", phone="+923001234568")),
        return_exceptions=True,
    )
    assert sum(isinstance(r, User) for r in results) == 1
    assert sum(isinstance(r, AdminExistsError) for r in results) == 1
    assert await _admins(use_test_db) == 1


@pytest.mark.db
async def test_email_already_used_by_another_account_is_a_conflict(
    use_test_db: async_sessionmaker[AsyncSession],
) -> None:
    async with use_test_db() as session:
        session.add(
            User(
                email="admin@example.com",
                hashed_password="x",
                role="customer",
                full_name="Someone",
                phone_number="+923009999999",
            )
        )
        await session.commit()
    with pytest.raises(ConflictError):
        await create_admin(_new())
    assert await _admins(use_test_db) == 0
