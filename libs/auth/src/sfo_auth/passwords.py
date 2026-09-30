from pwdlib import PasswordHash

# argon2 — passlib is effectively unmaintained, pwdlib is the maintained
# successor recommended for new FastAPI projects. Same primitive is reused for
# hashing refresh-token secrets (see tokens.py), not just passwords.
_hasher = PasswordHash.recommended()


def hash_secret(raw: str) -> str:
    return _hasher.hash(raw)


def verify_secret(raw: str, hashed: str) -> bool:
    return _hasher.verify(raw, hashed)
