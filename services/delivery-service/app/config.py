from sfo_common import BaseServiceSettings
from sfo_common.config import OutboxRelaySettings, TemporalSettings


class Settings(BaseServiceSettings, TemporalSettings, OutboxRelaySettings):
    mongo_url: str = "mongodb://mongo:27017"

    # Rider assignment (docs/week2-plan.md §7, D6). Placeholders like the
    # pricing values — confirm with the mentor.
    rider_search_radius_km: float = 5
    rider_offer_timeout_seconds: int = 60
    # Postgres keeps a rider's position at most this often; Redis has it live.
    rider_location_db_interval_seconds: int = 30
    # An assigned rider with no location update for this long is treated as
    # dropped out (D12) by the stale-rider detector.
    rider_stale_after_seconds: int = 180
    stale_rider_check_interval_seconds: float = 30


settings = Settings()  # type: ignore[call-arg]
