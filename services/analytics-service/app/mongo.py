"""The analytics read model (docs/week3-plan.md W3-D9) — MongoDB, rebuilt
from Kafka events; never the source of truth for anything.

- `order_facts`: one document per order (`_id` = order_id), timestamps of
  each milestone plus the dimensions metrics group by.
- `rider_shift_facts`: one document per RiderShiftChanged event.
- `failed_events`: one document per dead-lettered message.

Writes are upserts using only `$set`/`$min`/`$max`/`$addToSet` (or insert-
if-absent), so applying an event twice leaves the same document — the
consumer needs no processed-events table. Mongo here is a single node, so
there are no multi-document transactions to lean on anyway."""

from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.config import settings

ORDER_FACTS = "order_facts"
RIDER_SHIFT_FACTS = "rider_shift_facts"
FAILED_EVENTS = "failed_events"

# tz_aware: datetimes come back as UTC-aware, like everywhere else. 2s
# server selection: an outage fails a request fast instead of after 30s.
client: AsyncMongoClient = AsyncMongoClient(
    settings.mongo_url, serverSelectionTimeoutMS=2000, tz_aware=True
)
db: AsyncDatabase = client[settings.mongo_db]


async def ensure_indexes(database: AsyncDatabase = db) -> None:
    facts = database[ORDER_FACTS]
    await facts.create_index([("placed_at", ASCENDING)])
    await facts.create_index([("restaurant_id", ASCENDING), ("placed_at", ASCENDING)])
    # Ownership check for restaurant admins (app/api/routes.py).
    await facts.create_index([("restaurant_id", ASCENDING), ("restaurant_admin_id", ASCENDING)])
    # Rider utilization: deliveries active in a window.
    await facts.create_index(
        [("delivery_assigned_at", ASCENDING)],
        partialFilterExpression={"delivery_rider_id": {"$exists": True}},
    )
    await database[RIDER_SHIFT_FACTS].create_index([("rider_id", ASCENDING), ("at", ASCENDING)])
    await database[FAILED_EVENTS].create_index([("at", ASCENDING)])


async def check_ready() -> bool:
    try:
        await client.admin.command("ping")
        return True
    except Exception:
        return False
