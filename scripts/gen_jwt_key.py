"""Creates and rotates the Ed25519 key that signs access tokens (user-service only).

    uv run python scripts/gen_jwt_key.py            # first key
    uv run python scripts/gen_jwt_key.py --next     # rotation step 1
    uv run python scripts/gen_jwt_key.py --promote  # rotation step 2

Writes secrets/jwt_private_key.pem, which docker-compose mounts into
user-service as a secret (never into any other service), and
secrets/jwt_additional_public_keys.pem (public keys published next to the
current one; empty outside a rotation). Never overwrites the signing key in
place: replacing it makes every issued token unverifiable at once — rotate
instead (docs/architecture.md#access-token-signing). The secrets/ directory
is git-ignored; real environments use a secrets manager.
"""

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from sfo_auth.keys import TokenSigner, generate_private_key_pem, load_private_key

SECRETS = Path(__file__).resolve().parent.parent / "secrets"
CURRENT = SECRETS / "jwt_private_key.pem"
NEXT = SECRETS / "jwt_private_key.next.pem"
ADDITIONAL = SECRETS / "jwt_additional_public_keys.pem"


def _write_private(path: Path) -> str:
    path.write_bytes(generate_private_key_pem())
    # Readable by the container's non-root user (uid 10001) through the bind
    # mount; fine for local dev. Real deployments use a secrets manager.
    path.chmod(0o644)
    return _kid(path)


def _kid(path: Path) -> str:
    return TokenSigner(load_private_key(path.read_bytes())).kid


def _publish(private_key_path: Path) -> None:
    public_pem = (
        load_private_key(private_key_path.read_bytes())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    with ADDITIONAL.open("ab") as f:
        f.write(public_pem)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--next", action="store_true", help="create the next key and publish it")
    mode.add_argument("--promote", action="store_true", help="start signing with the next key")
    args = parser.parse_args()

    SECRETS.mkdir(exist_ok=True)
    # docker compose needs the file to exist even when there's nothing in it.
    ADDITIONAL.touch()

    if args.next:
        if not CURRENT.exists():
            print(f"{CURRENT} doesn't exist; create the first key without --next.", file=sys.stderr)
            return 1
        if NEXT.exists():
            print(f"{NEXT} already exists; run --promote next.", file=sys.stderr)
            return 1
        kid = _write_private(NEXT)
        _publish(NEXT)
        print(f"Wrote {NEXT} (key id: {kid}) and published its public key.")
        print("Restart user-service, wait 5 minutes, then run --promote.")
        return 0

    if args.promote:
        if not NEXT.exists():
            print(f"{NEXT} doesn't exist; run --next first.", file=sys.stderr)
            return 1
        _publish(CURRENT)  # tokens it signed must keep verifying until they expire
        NEXT.replace(CURRENT)
        print(f"Now signing with key id {_kid(CURRENT)}; the old key stays published.")
        print(f"Restart user-service; after 15 minutes empty {ADDITIONAL} and restart again.")
        return 0

    if CURRENT.exists():
        print(f"{CURRENT} already exists; not overwriting it.", file=sys.stderr)
        return 1
    print(f"Wrote {CURRENT} (key id: {_write_private(CURRENT)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
