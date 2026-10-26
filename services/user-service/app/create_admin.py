"""Creates the first system_admin. Run by an operator inside the user-service
container, not over HTTP:

    docker compose exec -T user-service python -m app.create_admin \\
        --email admin@example.com --full-name "Admin" --phone +923001234567 \\
        --password-stdin < password.txt

(without --password-stdin it prompts). scripts/seed.py runs it for the demo
admin; a real deployment runs it once after the first deploy (`docker exec`,
or a one-off Kubernetes Job).

Self-registration can't create a system_admin (PUBLIC_ROLES in
app/schemas.py), and there is no HTTP endpoint for it: only someone who can
already run commands in this container can create one, so there is nothing
reachable from outside to attack and no shared secret to manage. One-shot:
refuses once any system_admin exists, so it can't quietly add admins later.
Further admins are an explicit follow-up (an admin-only "promote user"
action), not this command.

Exit codes: 0 created, 3 a system_admin already exists (scripts/seed.py
treats that as done), 1 anything else."""

import argparse
import asyncio
import getpass
import sys

from pydantic import ValidationError
from sfo_auth import Role, hash_password
from sfo_common.errors import ConflictError
from sqlalchemy import text

from app import repository
from app.accounts import create_user_or_conflict
from app.db import SessionFactory, engine
from app.models import User
from app.schemas import NewAdmin

EXIT_ADMIN_EXISTS = 3

# Held for the transaction, so two runs at once can't both pass the
# "no admin yet" check. Distinct from sfo_common.db's migration lock key and
# app/maintenance.py's purge lock key.
_CREATE_ADMIN_LOCK_KEY = 727_003


class AdminExistsError(Exception):
    pass


async def create_admin(new: NewAdmin) -> User:
    async with SessionFactory() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CREATE_ADMIN_LOCK_KEY}
        )
        if await repository.any_user_with_role(session, Role.SYSTEM_ADMIN.value):
            raise AdminExistsError
        user = User(
            email=new.email,
            hashed_password=await hash_password(new.password),
            role=Role.SYSTEM_ADMIN,
            full_name=new.full_name,
            phone_number=new.phone_number,
        )
        # Commits, which also releases the lock.
        return await create_user_or_conflict(session, user)


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        # Only the line ending is dropped: spaces are part of a password.
        return sys.stdin.read().removesuffix("\n").removesuffix("\r")
    return getpass.getpass("Password for the new admin: ")


async def _run(new: NewAdmin) -> int:
    try:
        user = await create_admin(new)
    except AdminExistsError:
        print("A system_admin already exists; nothing to do.", file=sys.stderr)
        return EXIT_ADMIN_EXISTS
    except ConflictError as exc:
        print(f"Can't create the admin: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"Created system_admin {user.email} (id {user.id})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the first system_admin.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--phone", required=True, help="E.164, e.g. +923001234567")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting (never pass it as an argument)",
    )
    args = parser.parse_args()
    try:
        new = NewAdmin(
            email=args.email,
            password=_read_password(args.password_stdin),
            full_name=args.full_name,
            phone_number=args.phone,
        )
    except ValidationError as exc:
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            print(f"{field}: {error['msg']}", file=sys.stderr)
        return 1
    return asyncio.run(_run(new))


if __name__ == "__main__":
    raise SystemExit(main())
