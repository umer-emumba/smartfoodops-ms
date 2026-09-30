from typing import Self

from pydantic import model_validator
from sfo_common import BaseServiceSettings

# The dummy value shipped in env.example / scripts/seed.py — publicly known,
# so it must never be accepted outside local dev.
_DUMMY_BOOTSTRAP_SECRET = "dev-bootstrap-secret-change-me"


class Settings(BaseServiceSettings):
    # Shared secret for POST /v1/auth/bootstrap-admin — the only way a
    # system_admin account gets created, since self-registration is limited to
    # PUBLIC_ROLES. Dummy local value in env.example; a real deployment sets
    # this to a real secret and rotates/removes it after the first admin exists.
    admin_bootstrap_secret: str

    @model_validator(mode="after")
    def _refuse_dummy_secret_outside_local(self) -> Self:
        if self.environment != "local" and self.admin_bootstrap_secret == _DUMMY_BOOTSTRAP_SECRET:
            raise ValueError(
                "ADMIN_BOOTSTRAP_SECRET is the public dummy value — set a real secret "
                "for any environment other than local"
            )
        return self


settings = Settings()  # type: ignore[call-arg]  # loaded from env / .env at import time
