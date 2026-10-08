import pytest
from app.repository import _escape_like
from sfo_auth import (
    generate_refresh_token,
    hash_password,
    hash_refresh_secret,
    verify_password,
    verify_refresh_secret,
)


@pytest.mark.asyncio
async def test_password_hash_round_trip_off_event_loop() -> None:
    hashed = await hash_password("correct horse")
    assert hashed.startswith("$argon2")
    assert await verify_password("correct horse", hashed)
    assert not await verify_password("wrong horse", hashed)


def test_refresh_secret_uses_fast_hash() -> None:
    _, raw_secret, _ = generate_refresh_token()
    hashed = hash_refresh_secret(raw_secret)
    assert len(hashed) == 64  # SHA-256 hex, not argon2
    assert raw_secret not in hashed
    assert verify_refresh_secret(raw_secret, hashed)
    assert not verify_refresh_secret(raw_secret + "x", hashed)


def test_like_wildcards_are_escaped() -> None:
    assert _escape_like("a_b@x.com") == "a\\_b@x.com"
    assert _escape_like("50%") == "50\\%"
    assert _escape_like("back\\slash") == "back\\\\slash"
    assert _escape_like("plain") == "plain"
