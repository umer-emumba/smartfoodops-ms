"""Live rider positions in Redis (docs/week2-plan.md D6):

- `riders:available` — GEO set of riders who can take an offer right now;
  `offer_to_nearest_rider` searches it (GEOSEARCH, nearest first).
- `rider:{id}:location` — latest position of any rider on shift, available
  or not (ETA, live tracking). Expires after 10 minutes without updates.

Redis is a fast index, not the source of truth: whether a rider can really
be claimed is decided by an atomic UPDATE on `riders` in Postgres, so a
stale GEO entry can at worst cost one failed claim."""

import json
from datetime import UTC, datetime

from app import redis_client

AVAILABLE_KEY = "riders:available"
_LOCATION_TTL_SECONDS = 600


def _location_key(rider_id: str) -> str:
    return f"rider:{rider_id}:location"


async def record(rider_id: str, lat: float, lng: float, *, available: bool) -> None:
    redis = redis_client.redis
    async with redis.pipeline(transaction=False) as pipe:
        pipe.set(
            _location_key(rider_id),
            json.dumps({"lat": lat, "lng": lng, "at": datetime.now(UTC).isoformat()}),
            ex=_LOCATION_TTL_SECONDS,
        )
        if available:
            pipe.geoadd(AVAILABLE_KEY, [lng, lat, rider_id])
        else:
            pipe.zrem(AVAILABLE_KEY, rider_id)
        await pipe.execute()


async def latest(rider_id: str) -> tuple[float, float] | None:
    raw = await redis_client.redis.get(_location_key(rider_id))
    if raw is None:
        return None
    data = json.loads(raw)
    return data["lat"], data["lng"]


async def make_available(rider_id: str, fallback: tuple[float, float] | None = None) -> None:
    """Back into the searchable pool, at its latest known position (Redis,
    else `fallback` from Postgres). Without any position it can't be found by
    proximity, so it stays out until its next location update."""
    position = await latest(rider_id) or fallback
    if position is not None:
        await redis_client.redis.geoadd(AVAILABLE_KEY, [position[1], position[0], rider_id])


async def remove(rider_id: str) -> None:
    await redis_client.redis.zrem(AVAILABLE_KEY, rider_id)


async def nearest_available(
    lat: float, lng: float, radius_km: float, count: int = 20
) -> list[tuple[str, float]]:
    """(rider_id, km) within the radius, nearest first."""
    results = await redis_client.redis.geosearch(
        AVAILABLE_KEY,
        longitude=lng,
        latitude=lat,
        radius=radius_km,
        unit="km",
        sort="ASC",
        count=count,
        withdist=True,
    )
    # With withdist, each result is [member, distance]; the redis-py stubs
    # can't express that.
    return [(str(row[0]), float(row[1])) for row in results]  # type: ignore[index, arg-type]
