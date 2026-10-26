from sfo_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    # The private key that signs access tokens (Ed25519 PEM) — held by this
    # service only. Mounted as a Docker secret in compose; create it with
    # scripts/gen_jwt_key.py. Every other service gets the public half from
    # GET /.well-known/jwks.json (app/api/wellknown.py).
    jwt_private_key_file: str = "/run/secrets/jwt_private_key"
    # Optional PEM file of public keys published next to the signing key's:
    # during a rotation, the next key (so every service has it before any
    # token uses it) and then the previous one (until its tokens expire).
    jwt_additional_public_keys_file: str = ""

    # How often expired refresh-token rows are deleted (app/maintenance.py).
    refresh_token_purge_interval_seconds: int = 3600


settings = Settings()  # type: ignore[call-arg]  # loaded from env / .env at import time
