from sfo_common import BaseServiceSettings
from sfo_common.config import OutboxRelaySettings, TemporalSettings


class Settings(BaseServiceSettings, TemporalSettings, OutboxRelaySettings):
    pass


settings = Settings()  # type: ignore[call-arg]
