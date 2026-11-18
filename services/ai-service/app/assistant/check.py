"""The check step (docs/week5-plan.md B5-D3, B5-D5): code, not the model,
decides what reaches the customer. Every picked dish must be one of the
candidates the model was shown (no made-up IDs), orderable right now at its
live price, from a kitchen that isn't full; a meal must come from one
restaurant (an order is one restaurant) and fit the budget — added up here,
never by the model.

Pure functions over plain data, so each rule is tested without a model, a
Qdrant or restaurant-service."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.assistant.schemas import DishCard, Pick, RestaurantCard
from app.restaurant_client import LiveRestaurant
from app.retrieval import BUSY_AT, Load

MAX_OPTIONS = 5  # dishes shown as alternatives
MAX_MEAL_LINES = 8  # lines in one meal
MAX_QUANTITY = 10


@dataclass
class Checked:
    dishes: list[DishCard] = field(default_factory=list)
    restaurants: list[RestaurantCard] = field(default_factory=list)
    # What was wrong with the pick, in words the model can act on when it
    # is asked to pick again.
    problems: list[str] = field(default_factory=list)
    made_up: int = 0  # IDs that weren't among the candidates

    @property
    def total(self) -> float:
        return sum(c.price * c.quantity for c in self.dishes)


def rs(amount: float) -> str:
    return f"Rs {amount:,.0f}"


def check_dishes(
    pick: Pick,
    candidates: Mapping[str, dict[str, Any]],
    *,
    meal: bool,
    budget_pkr: float | None,
    max_each_pkr: float | None,
    live: Mapping[str, LiveRestaurant],
    loads: Mapping[str, Load],
) -> Checked:
    """`candidates`: item ID → its indexed data (what the model was shown).
    `meal`: the dishes are one order (a budget for the whole thing, or food
    for several people), not alternatives. `live` / `loads`: restaurant ID →
    live state; a restaurant missing from `live` is judged on the index's
    copy (its quote failed)."""
    out = Checked()
    seen: dict[str, DishCard] = {}
    for picked in pick.items:
        data = candidates.get(picked.item_id)
        if data is None:
            out.made_up += 1
            out.problems.append(f"{picked.item_id!r} is not one of the dishes you were given")
            continue
        name = str(data.get("name", picked.item_id))
        rid = str(data.get("restaurant_id", ""))
        price = float(data.get("price", 0))
        state = live.get(rid)
        if state is not None:
            item = state.items.get(picked.item_id)
            if not state.open:
                out.problems.append(f"{data.get('restaurant_name')} is not taking orders now")
                continue
            if item is None or not item.orderable:
                out.problems.append(f"{name} can't be ordered right now")
                continue
            price = float(item.price)
        load = loads.get(rid)
        if load is not None and load.full:
            out.problems.append(f"{data.get('restaurant_name')}'s kitchen is full right now")
            continue
        if not meal and max_each_pkr is not None and price > max_each_pkr:
            out.problems.append(f"{name} costs {rs(price)}, over {rs(max_each_pkr)} each")
            continue
        quantity = min(max(picked.quantity, 1), MAX_QUANTITY) if meal else 1
        if picked.item_id in seen:
            if meal:
                seen[picked.item_id].quantity = min(
                    seen[picked.item_id].quantity + quantity, MAX_QUANTITY
                )
            continue
        card = DishCard(
            item_id=picked.item_id,
            restaurant_id=rid,
            name=name,
            restaurant_name=str(data.get("restaurant_name", "")),
            price=price,
            quantity=quantity,
            reason=picked.reason.strip()[:200],
            spice_level=data.get("spice_level"),
            dietary_tags=list(data.get("dietary_tags") or []),
            busy=load is not None and load.utilization >= BUSY_AT,
        )
        seen[picked.item_id] = card
        out.dishes.append(card)

    limit = MAX_MEAL_LINES if meal else MAX_OPTIONS
    if len(out.dishes) > limit:
        out.dishes = out.dishes[:limit]
    if meal and out.dishes:
        places = {c.restaurant_id for c in out.dishes}
        if len(places) > 1:
            out.problems.append(
                "a meal is one order, so all its dishes must come from one restaurant"
            )
        if budget_pkr is not None and out.total > budget_pkr:
            out.problems.append(
                f"the meal adds up to {rs(out.total)}, over the budget of {rs(budget_pkr)}"
            )
    return out


def meal_is_valid(checked: Checked, budget_pkr: float | None) -> bool:
    """A meal that can be shown as is: one restaurant, within budget."""
    if not checked.dishes:
        return False
    one_place = len({c.restaurant_id for c in checked.dishes}) == 1
    return one_place and (budget_pkr is None or checked.total <= budget_pkr)


def check_restaurants(
    pick: Pick, candidates: Mapping[str, dict[str, Any]], reasons: Mapping[str, str]
) -> Checked:
    """Suggested restaurants must be among those retrieved (which already
    left out full kitchens, app/retrieval.py)."""
    out = Checked()
    for rid in dict.fromkeys(pick.restaurant_ids):
        data = candidates.get(rid)
        if data is None:
            out.made_up += 1
            out.problems.append(f"{rid!r} is not one of the restaurants you were given")
            continue
        out.restaurants.append(restaurant_card(rid, data, reasons.get(rid, "")))
    out.restaurants = out.restaurants[:MAX_OPTIONS]
    return out


def restaurant_card(rid: str, data: Mapping[str, Any], reason: str = "") -> RestaurantCard:
    load = data.get("load") or {}
    return RestaurantCard(
        restaurant_id=rid,
        name=str(data.get("name", "")),
        cuisine=data.get("cuisine"),
        avg_rating=data.get("avg_rating"),
        review_count=int(data.get("review_count") or 0),
        busy=bool(load.get("busy")),
        dishes=[str(d.get("name")) for d in data.get("dishes", [])][:3],
        reason=reason[:200],
    )


def dish_card(item_id: str, data: Mapping[str, Any]) -> DishCard:
    """A card straight from retrieval (the fallback when the model can't
    pick): index data, quantity 1, no reason."""
    return DishCard(
        item_id=item_id,
        restaurant_id=str(data.get("restaurant_id", "")),
        name=str(data.get("name", "")),
        restaurant_name=str(data.get("restaurant_name", "")),
        price=float(data.get("price", 0)),
        spice_level=data.get("spice_level"),
        dietary_tags=list(data.get("dietary_tags") or []),
    )


def fallback_meal(
    candidates: Mapping[str, dict[str, Any]], *, budget_pkr: float | None, people: int
) -> list[DishCard]:
    """A meal built in code when the model's picks keep failing the check:
    from one restaurant, the best-ranked dishes that still fit the budget —
    so even the fallback never breaks the rules a meal is checked against.
    The restaurant with the most such dishes wins (ties: search rank)."""
    by_restaurant: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for item_id, data in candidates.items():
        by_restaurant.setdefault(str(data.get("restaurant_id", "")), []).append((item_id, data))
    best: list[DishCard] = []
    for dishes in by_restaurant.values():
        meal: list[DishCard] = []
        total = 0.0
        for item_id, data in dishes:
            price = float(data.get("price", 0))
            if len(meal) >= max(people, 1) + 1:
                break
            if budget_pkr is not None and total + price > budget_pkr:
                continue
            meal.append(dish_card(item_id, data))
            total += price
        if len(meal) > len(best):
            best = meal
    return best
