import asyncio

from pwdlib import PasswordHash

# argon2 — passlib is effectively unmaintained, pwdlib is the maintained
# successor recommended for new FastAPI projects. Passwords only: refresh-token
# secrets are high-entropy random values and use a fast hash (see tokens.py).
_hasher = PasswordHash.recommended()

# One argon2 call is ~40-100ms of CPU and 64 MiB of memory. Run inline it would
# block the event loop, stalling every other request on this process; in a
# thread it doesn't (argon2-cffi releases the GIL). The semaphore caps how
# many run at once, so a login burst can't take memory to N x 64 MiB.
_MAX_CONCURRENT_HASHES = 4
_hash_slots = asyncio.Semaphore(_MAX_CONCURRENT_HASHES)


def hash_password_blocking(raw: str) -> str:
    """For import-time/one-off use only (e.g. a module-level constant) — never
    call this from a request handler; use hash_password."""
    return _hasher.hash(raw)


async def hash_password(raw: str) -> str:
    async with _hash_slots:
        return await asyncio.to_thread(_hasher.hash, raw)


async def verify_password(raw: str, hashed: str) -> bool:
    async with _hash_slots:
        return await asyncio.to_thread(_hasher.verify, raw, hashed)
