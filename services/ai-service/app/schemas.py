from typing import Any, Literal

from pydantic import BaseModel

DietaryTag = Literal["vegetarian", "vegan", "halal", "gluten_free", "dairy_free", "nut_free"]


class SearchHit(BaseModel):
    """`kind` item/restaurant/review/cuisine/order; `id` the entity's own ID
    (menu item, restaurant, reviewed order, cuisine name, order). `text` is
    the indexed document; `data` its fields (price, rating, location...).
    `score` orders the results (fused rank, not a probability)."""

    kind: str
    id: str
    score: float
    text: str
    data: dict[str, Any]


class SearchResponse(BaseModel):
    query: str
    kind: str
    hits: list[SearchHit]
