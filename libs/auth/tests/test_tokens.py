import asyncio
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sfo_auth import KeyStoreUnavailable, Role, StaticKeyStore, TokenSigner
from sfo_auth.keys import (
    ALGORITHM,
    ISSUER,
    JwksKeyStore,
    _raw_public_bytes,
    generate_private_key_pem,
    jwks_document,
    load_private_key,
    load_public_keys,
    parse_jwks,
    public_key_id,
)
from sfo_auth.tokens import create_access_token, decode_access_token


def _signer() -> TokenSigner:
    return TokenSigner(Ed25519PrivateKey.generate())


def _store(signer: TokenSigner) -> StaticKeyStore:
    return StaticKeyStore(signer.public_keys())


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": "u1",
        "role": "customer",
        "iat": now,
        "exp": now + 600,
        **overrides,
    }


async def test_round_trip() -> None:
    signer = _signer()
    token = create_access_token(user_id="u1", role=Role.RIDER, signer=signer, ttl_minutes=5)
    payload = await decode_access_token(token, keys=_store(signer))
    assert (payload.sub, payload.role) == ("u1", Role.RIDER)
    assert jwt.get_unverified_header(token) == {"alg": ALGORITHM, "typ": "JWT", "kid": signer.kid}


async def test_token_from_another_signer_is_rejected() -> None:
    trusted, attacker = _signer(), _signer()
    token = create_access_token(
        user_id="u1", role=Role.SYSTEM_ADMIN, signer=attacker, ttl_minutes=5
    )
    # Even claiming the trusted key's ID.
    forged = jwt.encode(
        jwt.decode(token, options={"verify_signature": False}),
        attacker.private_key,
        algorithm=ALGORITHM,
        headers={"kid": trusted.kid},
    )
    with pytest.raises(jwt.InvalidTokenError):
        await decode_access_token(forged, keys=_store(trusted))
    with pytest.raises(jwt.InvalidTokenError):  # unknown kid
        await decode_access_token(token, keys=_store(trusted))


async def test_algorithm_confusion_and_none_are_rejected() -> None:
    signer = _signer()
    hs256 = jwt.encode(
        _claims(),
        _raw_public_bytes(signer.public_key),
        algorithm="HS256",
        headers={"kid": signer.kid},
    )
    unsigned = jwt.encode(_claims(), "", algorithm="none", headers={"kid": signer.kid})
    for forged in (hs256, unsigned):
        with pytest.raises(jwt.InvalidTokenError):
            await decode_access_token(forged, keys=_store(signer))


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "someone-else"},
        {"exp": int(time.time()) - 10},
        {"role": "superuser"},
    ],
)
async def test_bad_claims_are_rejected(claims: dict[str, Any]) -> None:
    signer = _signer()
    token = jwt.encode(
        _claims(**claims), signer.private_key, algorithm=ALGORITHM, headers={"kid": signer.kid}
    )
    with pytest.raises(jwt.InvalidTokenError):
        await decode_access_token(token, keys=_store(signer))


async def test_missing_required_claims_are_rejected() -> None:
    signer = _signer()
    claims = _claims()
    del claims["iss"]
    token = jwt.encode(claims, signer.private_key, algorithm=ALGORITHM, headers={"kid": signer.kid})
    with pytest.raises(jwt.InvalidTokenError):
        await decode_access_token(token, keys=_store(signer))


def test_key_id_is_stable_and_jwks_round_trips() -> None:
    signer = _signer()
    assert signer.kid == public_key_id(signer.public_key)
    parsed = parse_jwks(signer.jwks())
    assert list(parsed) == [signer.kid]
    assert _raw_public_bytes(parsed[signer.kid]) == _raw_public_bytes(signer.public_key)
    assert "d" not in signer.jwks()["keys"][0]  # never the private half


def test_jwks_ignores_key_types_it_does_not_use() -> None:
    signer = _signer()
    document = jwks_document([signer.public_key])
    document["keys"].append({"kty": "RSA", "n": "x", "e": "AQAB", "kid": "rsa"})  # type: ignore[arg-type]
    assert list(parse_jwks(document)) == [signer.kid]


def test_jwks_skips_malformed_keys_and_keeps_the_good_ones() -> None:
    signer = _signer()
    document = jwks_document([signer.public_key])
    document["keys"].append({"kty": "OKP", "crv": "Ed25519", "x": "AAAA"})  # too short
    document["keys"].append({"kty": "OKP", "crv": "Ed25519", "x": 42})  # type: ignore[dict-item]
    assert list(parse_jwks(document)) == [signer.kid]


async def test_small_clock_skew_is_tolerated() -> None:
    signer = _signer()
    now = int(time.time())
    token = jwt.encode(
        _claims(iat=now + 5), signer.private_key, algorithm=ALGORITHM, headers={"kid": signer.kid}
    )
    assert (await decode_access_token(token, keys=_store(signer))).sub == "u1"


def test_private_key_pem_round_trip_and_rejects_other_key_types() -> None:
    key = load_private_key(generate_private_key_pem())
    assert isinstance(key, Ed25519PrivateKey)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_pem = rsa.generate_private_key(65537, 2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValueError):
        load_private_key(rsa_pem)


def test_rotation_publishes_additional_keys_until_removed(tmp_path: Any) -> None:
    from cryptography.hazmat.primitives import serialization

    old, new = _signer(), _signer()
    additional = tmp_path / "additional.pem"
    additional.write_bytes(
        old.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    key_file = tmp_path / "private.pem"
    key_file.write_bytes(
        new.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    signer = TokenSigner.from_files(str(key_file), str(additional))
    assert signer.kid == new.kid
    assert set(signer.public_keys()) == {old.kid, new.kid}
    assert len(load_public_keys(additional.read_bytes())) == 1


class _FakeJwks:
    def __init__(self, *signers: TokenSigner) -> None:
        self.document = jwks_document(k for s in signers for k in [s.public_key])
        self.calls = 0
        self.down = False

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self.down:
            raise ConnectionError("user-service is down")
        return self.document


async def test_jwks_store_caches_and_refreshes_after_ttl() -> None:
    signer = _signer()
    fake = _FakeJwks(signer)
    store = JwksKeyStore("http://x", ttl_seconds=0.05, min_refresh_seconds=0, fetch=fake)
    assert await store.get(signer.kid) is not None
    assert await store.get(signer.kid) is not None
    assert fake.calls == 1  # second lookup served from the cache
    time.sleep(0.06)
    assert await store.get(signer.kid) is not None
    await store._background  # the refresh ran behind the scenes
    assert fake.calls == 2


async def test_stale_key_is_served_without_waiting_for_the_refresh() -> None:
    signer = _signer()
    fake = _FakeJwks(signer)
    store = JwksKeyStore("http://x", ttl_seconds=0.05, min_refresh_seconds=0, fetch=fake)
    await store.get(signer.kid)
    release = asyncio.Event()

    async def slow_fetch() -> dict[str, Any]:
        await release.wait()  # user-service hanging
        return fake.document

    store._fetch = slow_fetch
    time.sleep(0.06)
    # Answered while the refresh is still stuck, and repeated lookups don't
    # start a second refresh.
    assert await store.get(signer.kid) is not None
    assert await store.get(signer.kid) is not None
    task = store._background
    assert task is not None and not task.done()
    release.set()
    await task
    assert store._fresh(time.monotonic())


async def test_unknown_kid_refetches_at_most_once_per_interval() -> None:
    signer, rotated = _signer(), _signer()
    fake = _FakeJwks(signer)
    store = JwksKeyStore("http://x", min_refresh_seconds=60, fetch=fake)
    assert await store.get(signer.kid) is not None
    assert await store.get("random-1") is None
    assert await store.get("random-2") is None
    assert fake.calls == 1  # the second and third were throttled, not fetched

    # A newly rotated-in key is picked up once the interval has passed.
    fake.document = jwks_document([signer.public_key, rotated.public_key])
    store._last_attempt = time.monotonic() - 61
    assert await store.get(rotated.kid) is not None
    assert fake.calls == 2


async def test_stale_keys_are_served_when_refresh_fails() -> None:
    signer = _signer()
    fake = _FakeJwks(signer)
    store = JwksKeyStore("http://x", ttl_seconds=0.05, min_refresh_seconds=0, fetch=fake)
    await store.get(signer.kid)
    fake.down = True
    time.sleep(0.06)
    assert await store.get(signer.kid) is not None  # stale, but still usable
    await store._background
    assert await store.get(signer.kid) is not None  # still, after the refresh failed


async def test_unavailable_when_nothing_cached_or_too_stale() -> None:
    signer = _signer()
    fake = _FakeJwks(signer)
    fake.down = True
    store = JwksKeyStore("http://x", min_refresh_seconds=0, fetch=fake)
    with pytest.raises(KeyStoreUnavailable):
        await store.get(signer.kid)

    fake.down = False
    store = JwksKeyStore(
        "http://x", ttl_seconds=0.01, max_stale_seconds=0.05, min_refresh_seconds=0, fetch=fake
    )
    await store.get(signer.kid)
    fake.down = True
    time.sleep(0.06)
    with pytest.raises(KeyStoreUnavailable):
        await store.get(signer.kid)


async def test_recovers_quickly_when_the_first_fetch_failed() -> None:
    signer = _signer()
    fake = _FakeJwks(signer)
    fake.down = True
    store = JwksKeyStore("http://x", min_refresh_seconds=60, retry_seconds=0.05, fetch=fake)
    with pytest.raises(KeyStoreUnavailable):
        await store.get(signer.kid)
    fake.down = False
    time.sleep(0.06)
    # Not held back by the 60s unknown-key throttle: nothing was ever loaded.
    assert await store.get(signer.kid) is not None
