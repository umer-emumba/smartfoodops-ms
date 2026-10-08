from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from sfo_common import new_id

from app.config import settings

# 2s server-selection timeout, not Motor's 30s default: a Mongo outage should
# cost a location update 2s (the ping is best-effort), not stall it for 30s.
_client: AsyncIOMotorClient = AsyncIOMotorClient(settings.mongo_url, serverSelectionTimeoutMS=2000)
db: AsyncIOMotorDatabase = _client["delivery_db"]

# High-frequency rider location pings live here, not in Postgres — see
# docs/architecture.md#services. riders.last_lat/last_lng (Postgres) is only
# ever the latest known position for assignment queries; this collection is
# the full ping history. TTL-indexed at 24h: enough to replay/debug a single
# delivery's tracking, not a standing analytics store (that's
# analytics-service's Mongo, Week 3).
RIDER_LOCATION_PINGS = "rider_location_pings"
_PING_TTL_SECONDS = 24 * 60 * 60


async def ensure_indexes() -> None:
    await db[RIDER_LOCATION_PINGS].create_index("recorded_at", expireAfterSeconds=_PING_TTL_SECONDS)
    await db[RIDER_LOCATION_PINGS].create_index("rider_id")


async def check_mongo_ready() -> bool:
    try:
        await _client.admin.command("ping")
        return True
    except Exception:
        return False


async def insert_location_ping(rider_id: str, lat: float, lng: float) -> None:
    # _id is sfo_common.new_id() (UUIDv7), not Mongo's default ObjectId — one
    # consistent, time-ordered ID scheme across Postgres rows, Kafka events,
    # and Mongo documents (docs/technical-documentation.md, "Primary keys: UUIDv7...").
    await db[RIDER_LOCATION_PINGS].insert_one(
        {
            "_id": new_id(),
            "rider_id": rider_id,
            "lat": lat,
            "lng": lng,
            "recorded_at": datetime.now(UTC),
        }
    )
