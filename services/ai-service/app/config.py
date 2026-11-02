from pydantic import SecretStr
from sfo_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    # Vector store (docs/week4-plan.md B4-D2). Short timeout: a Qdrant outage
    # fails a search or readiness check fast instead of hanging the request.
    qdrant_url: str = "http://qdrant:6333"
    qdrant_timeout_seconds: int = 5

    # Claude (Week 5). Empty until the user adds a key to .env; SecretStr keeps
    # it out of logs and reprs.
    anthropic_api_key: SecretStr = SecretStr("")


settings = Settings()  # type: ignore[call-arg]
