"""What the assistant's model calls return, and what a turn produces.

The model-facing schemas (`Understanding`, `Pick`) are plain — no numeric
or length limits in the JSON schema, which providers' structured-output
modes support unevenly (B5-D1). Code clamps and checks the values instead
(`Understanding.cleaned`, app/assistant/check.py): the model is never
trusted to stay in range."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import DietaryTag

Intent = Literal[
    "recommend_dishes",  # what to eat: options, a meal, a combo
    "suggest_restaurants",  # where to order from
    "order_history",  # their own past orders: "my usual", "what did I get last time"
    "order_help",  # an order's status, delay, ETA, refund, cancellation
    "greeting",
    "out_of_scope",
]
INTENTS: tuple[str, ...] = Intent.__args__  # type: ignore[attr-defined]


def _lists_may_be_null(schema: dict[str, Any]) -> None:
    """In the JSON schema sent to the model, a list of plain values may also
    be null. gpt-oss on Groq answers `"dietary": null` for "none", and Groq
    rejects output that breaks the schema (400). Lists of objects (`$ref`)
    stay arrays: qwen3 answered null to every nullable `$ref` (B5-D17)."""
    for prop in schema.get("properties", {}).values():
        if prop.get("type") == "array" and "$ref" not in prop.get("items", {}):
            prop["type"] = ["array", "null"]


class ModelFacing(BaseModel):
    """A schema the model fills in: a null list reads as empty."""

    model_config = ConfigDict(json_schema_extra=_lists_may_be_null)

    @model_validator(mode="before")
    @classmethod
    def _null_lists_are_empty(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for name, field in cls.model_fields.items():
                if (
                    data.get(name, ...) is None
                    and getattr(field.annotation, "__origin__", None) is list
                ):
                    data = {**data, name: []}
        return data


class Constraints(ModelFacing):
    # Flat numbers, not a nested money object: local models (qwen3 on
    # Ollama) answered a nested `{amount, currency} | null` with null every
    # time — 0 of 5 budgets on the eval.
    budget_total: float | None = Field(
        default=None, description="The most the whole order may cost, if the customer said so."
    )
    max_price_each: float | None = Field(
        default=None, description="A price cap per dish, if the customer said so."
    )
    currency: Literal["PKR", "USD"] = Field(
        default="PKR", description="USD only if the customer gave the amount in dollars."
    )
    people: int | None = Field(default=None, description="How many people the food is for.")
    dietary: list[DietaryTag] = Field(default_factory=list)
    max_spice: int | None = Field(
        default=None, description="0 not spicy, 1 mild, 2 spicy, 3 very spicy."
    )
    cuisines: list[str] = Field(
        default_factory=list, description='Cuisine words, lower case: "thai", "bbq", "chinese".'
    )
    exclude: list[str] = Field(
        default_factory=list,
        description='Ingredients or dish words to avoid, singular, lower case: "beef", "egg".',
    )
    min_rating: float | None = Field(default=None, description="Lowest restaurant rating, 1–5.")


class Understanding(ModelFacing):
    """The fast model's one call per message (B5-D4): what is being asked,
    a clean English search query, and the constraints as filters."""

    intent: Intent
    search_query: str = Field(
        description=(
            "What to search the menu for, in short English: dish, ingredients, style. "
            "Translate Roman Urdu. Leave out negations, prices and other constraints "
            "(they go in `constraints`)."
        )
    )
    constraints: Constraints = Field(default_factory=Constraints)
    this_restaurant: bool = Field(
        default=False,
        description="True if the customer means the restaurant they are looking at.",
    )
    refers_to_previous: bool = Field(
        default=False,
        description="True if the message builds on the previous answer (cheaper, another one).",
    )

    def cleaned(self, max_query_chars: int = 120) -> "Understanding":
        """Values clamped to what the filters accept."""
        c = self.constraints

        def money(amount: float | None) -> float | None:
            return amount if amount is not None and 0 < amount < 1_000_000 else None

        def words(ws: list[str], n: int) -> list[str]:
            out = [w.strip().lower()[:40] for w in ws if w.strip()]
            return list(dict.fromkeys(out))[:n]

        constraints = Constraints(
            budget_total=money(c.budget_total),
            max_price_each=money(c.max_price_each),
            currency=c.currency,
            people=c.people if c.people is not None and 1 <= c.people <= 20 else None,
            dietary=list(dict.fromkeys(c.dietary)),
            max_spice=None if c.max_spice is None else min(max(c.max_spice, 0), 3),
            cuisines=words(c.cuisines, 3),
            exclude=words(c.exclude, 8),
            # Below 1 isn't a rating anyone asks for: a model filling the field.
            min_rating=None if c.min_rating is None or c.min_rating < 1 else min(c.min_rating, 5.0),
        )
        query = " ".join(self.search_query.split())[:max_query_chars]
        return self.model_copy(update={"constraints": constraints, "search_query": query})


class PickedItem(ModelFacing):
    item_id: str = Field(description='The dish\'s code in brackets, e.g. "D3".')
    quantity: int = 1
    reason: str = Field(description="Why this dish, in a few words, from the given facts.")


class Pick(ModelFacing):
    """The smart model's choice among the candidates it was given (B5-D3):
    IDs only — names, prices and everything shown come from our data."""

    items: list[PickedItem] = Field(default_factory=list)
    restaurant_ids: list[str] = Field(
        default_factory=list, description='Restaurant codes in brackets, e.g. "R1".'
    )
    no_match: bool = Field(
        default=False, description="True if nothing given fits what the customer asked for."
    )
    note: str = Field(
        default="",
        description="One short sentence: a constraint you had to relax, or why nothing fits.",
    )


# --- What a turn produces (shown to the customer). ---


class DishCard(BaseModel):
    item_id: str
    restaurant_id: str
    name: str
    restaurant_name: str
    price: float  # PKR, live when the quote succeeded
    quantity: int = 1
    reason: str = ""
    spice_level: int | None = None
    dietary_tags: list[str] = Field(default_factory=list)
    busy: bool = False  # the kitchen is nearly full right now


class RestaurantCard(BaseModel):
    restaurant_id: str
    name: str
    cuisine: str | None = None
    avg_rating: float | None = None
    review_count: int = 0
    busy: bool = False
    dishes: list[str] = Field(default_factory=list)  # names of its matching dishes
    reason: str = ""


TurnStatus = Literal["answered", "refused", "degraded"]
