from sfo_common import BaseServiceSettings
from sfo_common.config import TemporalSettings


class Settings(BaseServiceSettings, TemporalSettings):
    mongo_url: str = "mongodb://mongo:27017"


settings = Settings()  # type: ignore[call-arg]
