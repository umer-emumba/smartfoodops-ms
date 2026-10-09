from sfo_common import BaseServiceSettings
from sfo_common.config import TemporalSettings


class Settings(BaseServiceSettings, TemporalSettings):
    pass


settings = Settings()  # type: ignore[call-arg]
