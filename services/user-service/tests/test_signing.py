from collections.abc import Iterator
from pathlib import Path

import pytest
from app.config import settings
from app.main import app
from app.signing import OwnKeys, get_signer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role, create_access_token, decode_access_token
from sfo_auth.keys import generate_private_key_pem, public_key_id


@pytest.fixture
def key_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throwaway signing key plus one additional (next/previous) public key."""
    private = tmp_path / "private.pem"
    private.write_bytes(generate_private_key_pem())
    additional = tmp_path / "additional.pem"
    additional.write_bytes(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    monkeypatch.setattr(settings, "jwt_private_key_file", str(private))
    monkeypatch.setattr(settings, "jwt_additional_public_keys_file", str(additional))
    get_signer.cache_clear()
    yield additional
    get_signer.cache_clear()


async def test_jwks_publishes_public_keys_only(key_files: Path) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    keys = response.json()["keys"]
    assert {k["kid"] for k in keys} == set(get_signer().public_keys())
    assert len(keys) == 2  # signing key + the additional one
    assert all("d" not in k for k in keys)  # never the private half


async def test_own_tokens_verify_against_own_keys(key_files: Path) -> None:
    token = create_access_token(
        user_id="u1", role=Role.CUSTOMER, signer=get_signer(), ttl_minutes=5
    )
    payload = await decode_access_token(token, keys=OwnKeys())
    assert (payload.sub, payload.role) == ("u1", Role.CUSTOMER)
    assert get_signer().kid == public_key_id(get_signer().public_key)


def test_missing_signing_key_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_private_key_file", str(tmp_path / "missing.pem"))
    get_signer.cache_clear()
    try:
        with pytest.raises(FileNotFoundError):
            get_signer()
    finally:
        get_signer.cache_clear()
