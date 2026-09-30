from sfo_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    mongo_url: str = "mongodb://mongo:27017"


settings = Settings()  # type: ignore[call-arg]
