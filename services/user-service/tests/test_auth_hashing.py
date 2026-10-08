from datetime import UTC, datetime

from app.repository import _escape_like, purge_expired_refresh_tokens
from sfo_auth import (
    generate_refresh_token,
    hash_password,
    hash_refresh_secret,
    verify_password,
    verify_refresh_secret,
)
from sqlalchemy.dialects import postgresql


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


class _RecordingSession:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return type("Result", (), {"rowcount": 7})()


async def test_purge_deletes_only_expired_rows_in_a_bounded_batch() -> None:
    session = _RecordingSession()
    deleted = await purge_expired_refresh_tokens(
        session, now=datetime(2026, 10, 8, tzinfo=UTC), batch_size=1000
    )
    assert deleted == 7
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert sql.startswith("DELETE FROM refresh_tokens")
    assert "refresh_tokens.expires_at <" in sql
    # Revoked-but-unexpired rows stay: reuse detection needs them.
    assert "revoked_at" not in sql
    assert "LIMIT" in sql
