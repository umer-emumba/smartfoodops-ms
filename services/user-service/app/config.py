from sfo_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    # Shared secret for POST /v1/auth/bootstrap-admin — the only way a
    # system_admin account gets created, since self-registration is limited to
    # PUBLIC_ROLES. Dummy local value in env.example; a real deployment sets
    # this to a real secret and rotates/removes it after the first admin exists.
    admin_bootstrap_secret: str


settings = Settings()  # type: ignore[call-arg]  # loaded from env / .env at import time
