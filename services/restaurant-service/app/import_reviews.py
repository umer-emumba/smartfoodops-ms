"""Imports historical reviews — demo data only. Run inside the
restaurant-service container, not over HTTP:

    docker compose exec -T restaurant-service python -m app.import_reviews < reviews.json

scripts/seed.py builds the JSON (a list of ImportedReview) from
scripts/fixtures/catalog.json. Each review belongs to a delivered order
that order-service's own import command (app/import_history.py) created
with the same ID, so the data stays consistent across both services.

Unlike POST /v1/restaurants/{id}/reviews, nothing is checked with
order-service and created_at is taken from the input (the history spans
past months). Re-runnable: a review whose order already has one is skipped,
and each touched restaurant's rating aggregate is rebuilt from its reviews.

Exit codes: 0 done, 1 invalid input."""

import asyncio
import sys

from pydantic import AwareDatetime, BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy.dialects.postgresql import insert

from app import cache, repository
from app.db import SessionFactory, engine
from app.models import Review


class ImportedReview(BaseModel):
    order_id: str
    customer_id: str
    restaurant_id: str
    rating: int = Field(ge=1, le=5)
    text: str = Field(min_length=1, max_length=4000)
    item_ids: list[str]
    created_at: AwareDatetime


_reviews_json = TypeAdapter(list[ImportedReview])


async def import_reviews(reviews: list[ImportedReview]) -> int:
    """Returns how many were new."""
    async with SessionFactory() as session:
        inserted = 0
        for review in reviews:
            result = await session.execute(
                insert(Review)
                .values(**review.model_dump())
                .on_conflict_do_nothing(index_elements=[Review.order_id])
            )
            inserted += result.rowcount
        restaurant_ids = sorted({r.restaurant_id for r in reviews})
        for restaurant_id in restaurant_ids:
            await repository.recompute_rating(session, restaurant_id)
        await session.commit()
    for restaurant_id in restaurant_ids:
        await cache.invalidate(restaurant_id)
    return inserted


async def _run(reviews: list[ImportedReview]) -> int:
    try:
        inserted = await import_reviews(reviews)
    finally:
        await engine.dispose()
    print(f"Imported {inserted} new reviews ({len(reviews) - inserted} already there)")
    return 0


def main() -> int:
    try:
        reviews = _reviews_json.validate_json(sys.stdin.buffer.read())
    except ValidationError as exc:
        print(exc, file=sys.stderr)
        return 1
    return asyncio.run(_run(reviews))


if __name__ == "__main__":
    raise SystemExit(main())
