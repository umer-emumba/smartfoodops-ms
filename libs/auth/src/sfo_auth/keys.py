"""Signing keys for access tokens: asymmetric, so only user-service can issue.

Access tokens are signed with Ed25519 (JWT alg `EdDSA`). user-service holds
the private key and publishes the public keys as a JWKS document
(`GET /.well-known/jwks.json`). Every other service only ever has public
keys, so a compromised service can read tokens but can't forge one — unlike
the shared HS256 secret this replaced.

Each token's header carries a `kid` (key ID: the RFC 7638 thumbprint of the
public key), so keys can rotate: publish the new key next to the old one
before signing with it, switch signing to the new one, and drop the old one
after 15 minutes (the access-token lifetime). See
docs/architecture.md#access-token-signing.

Verifiers use a `KeyStore`: `JwksKeyStore` for services that fetch the JWKS
from user-service, `StaticKeyStore` where the keys are already in hand
(user-service itself, tests)."""

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

logger = logging.getLogger(__name__)

# Fixed on purpose: a verifier that accepted whatever the token's header says
# could be tricked into the wrong algorithm (e.g. "none", or HS256 with the
# public key as the secret).
ALGORITHM = "EdDSA"
ISSUER = "smartfoodops-user-service"

_END_PUBLIC_KEY = "-----END PUBLIC KEY-----"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _raw_public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def generate_private_key_pem() -> bytes:
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("The JWT signing key must be an Ed25519 private key")
    return key


def load_public_keys(pem_bundle: bytes) -> list[Ed25519PublicKey]:
    """A file with one or more `-----BEGIN PUBLIC KEY-----` blocks."""
    keys: list[Ed25519PublicKey] = []
    for block in pem_bundle.decode().split(_END_PUBLIC_KEY):
        if "BEGIN PUBLIC KEY" not in block:
            continue
        key = serialization.load_pem_public_key((block + _END_PUBLIC_KEY).strip().encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("Additional JWT keys must be Ed25519 public keys")
        keys.append(key)
    return keys


def public_key_id(key: Ed25519PublicKey) -> str:
    """RFC 7638 JWK thumbprint: the same key always gets the same ID, so no
    one has to configure or keep key IDs in sync."""
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": _b64url(_raw_public_bytes(key))},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def public_jwk(key: Ed25519PublicKey) -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _b64url(_raw_public_bytes(key)),
        "kid": public_key_id(key),
        "use": "sig",
        "alg": ALGORITHM,
    }


def jwks_document(keys: Iterable[Ed25519PublicKey]) -> dict[str, list[dict[str, str]]]:
    return {"keys": [public_jwk(key) for key in keys]}


def parse_jwks(document: dict[str, Any]) -> dict[str, Ed25519PublicKey]:
    """kid → public key. Entries that aren't Ed25519 signing keys, or are
    malformed, are skipped: one bad entry mustn't throw away the good keys
    (and with them every token's verification)."""
    keys: dict[str, Ed25519PublicKey] = {}
    for jwk in document.get("keys", []):
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or "x" not in jwk:
            continue
        try:
            key = Ed25519PublicKey.from_public_bytes(_b64url_decode(jwk["x"]))
        except (TypeError, ValueError):
            logger.warning("Skipping a malformed key in the JWKS")
            continue
        keys[public_key_id(key)] = key  # computed, not trusted from the document
    return keys


class TokenSigner:
    """user-service's signing side: the current private key, plus additional
    public keys published alongside it — the next key (published before
    signing switches to it, so verifiers already have it) and the previous
    one (kept until tokens signed with it expire)."""

    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        additional_public_keys: Iterable[Ed25519PublicKey] = (),
    ) -> None:
        self.private_key = private_key
        self.public_key = private_key.public_key()
        self.kid = public_key_id(self.public_key)
        self._additional = tuple(additional_public_keys)

    @classmethod
    def from_files(
        cls, private_key_file: str, additional_public_keys_file: str = ""
    ) -> "TokenSigner":
        private_key = load_private_key(Path(private_key_file).read_bytes())
        additional = (
            load_public_keys(Path(additional_public_keys_file).read_bytes())
            if additional_public_keys_file
            else []
        )
        return cls(private_key, additional)

    def public_keys(self) -> dict[str, Ed25519PublicKey]:
        keys = [self.public_key, *self._additional]
        return {public_key_id(key): key for key in keys}

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        return jwks_document(self.public_keys().values())


class KeyStoreUnavailable(Exception):
    """The public keys can't be obtained right now (user-service unreachable
    and nothing cached). Callers answer 503, not 401: the token may be fine."""


class KeyStore(Protocol):
    async def get(self, kid: str) -> Ed25519PublicKey | None: ...


class StaticKeyStore:
    def __init__(self, keys: dict[str, Ed25519PublicKey]) -> None:
        self._keys = keys

    async def get(self, kid: str) -> Ed25519PublicKey | None:
        return self._keys.get(kid)


class JwksKeyStore:
    """Public keys fetched from user-service's JWKS endpoint and cached.

    - Nothing is fetched at start-up: the first token triggers the first
      fetch, so a service doesn't need user-service to be up to boot.
    - Refreshed after `ttl_seconds`, so a rotated-out key stops being
      accepted within that time. The refresh runs in the background: a
      token whose key is cached is verified straight away, never made to
      wait for user-service.
    - A token with an unknown `kid` triggers a refresh (a freshly rotated
      key), but at most one per `min_refresh_seconds`, so random key IDs
      can't turn into a flood of requests to user-service. Until the first
      successful fetch there is nothing to protect, so retries are only
      spaced `retry_seconds` apart and recovery is quick once user-service
      is back.
    - If a refresh fails, the cached keys keep working for up to
      `max_stale_seconds`; with nothing usable cached, `KeyStoreUnavailable`.
    """

    _shared: dict[str, "JwksKeyStore"] = {}

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float = 300,
        min_refresh_seconds: float = 30,
        retry_seconds: float = 2,
        max_stale_seconds: float = 3600,
        timeout_seconds: float = 2.0,
        fetch: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._retry = retry_seconds
        self._max_stale = max_stale_seconds
        self._timeout = timeout_seconds
        self._fetch = fetch or self._fetch_over_http
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._loaded_at: float | None = None
        self._last_attempt: float | None = None
        self._pinned = False
        self._lock = asyncio.Lock()
        self._background: asyncio.Task[None] | None = None

    @classmethod
    def shared(cls, url: str) -> "JwksKeyStore":
        """One store per JWKS URL in the process, so the gateway's and the
        dependencies' lookups share one cache."""
        if url not in cls._shared:
            cls._shared[url] = cls(url)
        return cls._shared[url]

    def pin(self, keys: dict[str, Ed25519PublicKey]) -> None:
        """Use exactly these keys and never fetch (tests)."""
        self._keys = dict(keys)
        self._loaded_at = time.monotonic()
        self._pinned = True

    async def _fetch_over_http(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(self._url)
            response.raise_for_status()
            return response.json()

    def _fresh(self, now: float) -> bool:
        return self._loaded_at is not None and now - self._loaded_at < self._ttl

    def _usable(self, now: float) -> bool:
        return self._loaded_at is not None and now - self._loaded_at <= self._max_stale

    def _may_refresh(self, now: float) -> bool:
        spacing = self._min_refresh if self._loaded_at is not None else self._retry
        return self._last_attempt is None or now - self._last_attempt >= spacing

    async def get(self, kid: str) -> Ed25519PublicKey | None:
        now = time.monotonic()
        if self._pinned or (kid in self._keys and self._fresh(now)):
            return self._keys.get(kid)
        if kid in self._keys and self._usable(now):
            # Cached but past its TTL: answer now, refresh behind the scenes.
            self._refresh_in_background(now)
            return self._keys[kid]
        # Unknown kid, or nothing usable cached: this one has to wait.
        async with self._lock:
            now = time.monotonic()
            # Another request may have refreshed while this one waited.
            if kid in self._keys and self._fresh(now):
                return self._keys[kid]
            if self._may_refresh(now):
                await self._refresh()
            if not self._usable(time.monotonic()):
                raise KeyStoreUnavailable("JWT public keys are unavailable")
            return self._keys.get(kid)

    def _refresh_in_background(self, now: float) -> None:
        if (self._background is not None and not self._background.done()) or not (
            self._may_refresh(now)
        ):
            return
        self._last_attempt = now  # so concurrent requests don't start another
        self._background = asyncio.create_task(self._locked_refresh())

    async def _locked_refresh(self) -> None:
        async with self._lock:
            if not self._fresh(time.monotonic()):
                await self._refresh()

    async def _refresh(self) -> None:
        self._last_attempt = time.monotonic()
        try:
            keys = parse_jwks(await self._fetch())
            if not keys:
                raise ValueError("the JWKS has no usable keys")
        except Exception:
            logger.warning("JWKS refresh from %s failed", self._url, exc_info=True)
            return
        self._keys = keys
        self._loaded_at = time.monotonic()
