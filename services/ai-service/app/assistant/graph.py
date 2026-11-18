"""The assistant's turn as a fixed LangGraph pipeline (docs/week5-plan.md
B5-D2, B5-D3):

    understand ─┬─ retrieve ─ pick ─ check ─┬─ explain
                │               ▲           │
                │               └─ retry ───┘ (once)
                └─ reply  (greeting, out of scope, order help)

- understand (fast model, one call, B5-D4): intent, an English search
  query, constraints as filters; a dollar budget converted to PKR.
- retrieve (no model): dishes, restaurant suggestions or past orders, plus
  the customer's taste profile, in parallel.
- pick (smart model): IDs only, from the candidates shown.
- check (code, app/assistant/check.py): IDs, live prices, availability,
  kitchen load, one restaurant per meal, the budget. A failed check sends
  the problems back to pick once; then the valid part, or plain search
  results.
- explain (fast model, streamed): the words, from the checked cards only.

Progress goes out through LangGraph's stream writer as events — `status`,
`cards` (before any text, so the answer feels fast), `token` — which step 3
turns into SSE. Without an LLM (no key, provider down) every model step has
a code fallback: the customer still gets search results, marked degraded,
never an error.

The graph knows nothing about HTTP or the database: memory comes in as
state and the outcome goes out as state (app/assistant/service.py)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app import metrics
from app.assistant import check as checks
from app.assistant.schemas import (
    Constraints,
    DishCard,
    Pick,
    RestaurantCard,
    TurnStatus,
    Understanding,
)
from app.config import settings
from app.llm import LLM, LLMError, map_errors
from app.prompts import fence, load
from app.restaurant_client import LiveRestaurant
from app.retrieval import Filters, Hit, Load, Retriever

logger = logging.getLogger(__name__)

# What the customer sees when the model step it needed isn't available.
DEGRADED_TEXT = (
    "I can't write a full answer right now, so here are the closest matches to your words. "
    "Please check spice, diet and price on each before ordering."
)
NOTHING_FOUND = "Nothing on the menu matches that right now."
CANNED = {
    "greeting": (
        "Hi! Tell me what you feel like eating — a dish, a budget, a cuisine — "
        "and I'll find something on the menu."
    ),
    "out_of_scope": "I can only help with food, restaurants and orders on SmartFoodOps.",
    "order_help": (
        'For an order\'s status or a delay, open the order and tap "Why is my order late?" — '
        "it explains that order from its live details."
    ),
}
SEARCH_DOWN = "Search is unavailable right now. Please try again in a minute."


@dataclass
class Deps:
    """Everything a turn uses from outside — replaced wholesale in tests."""

    llm: LLM
    retriever: Retriever
    quotes: Callable[[dict[str, list[str]]], Awaitable[dict[str, LiveRestaurant]]]
    loads: Callable[[Sequence[str]], Awaitable[dict[str, Load]]]


class TurnState(TypedDict, total=False):
    # In.
    user_id: str
    message: str  # redacted
    history: list[dict[str, str]]  # recent turns: {role, content}
    summary: str  # older turns, folded
    previous: dict[str, Any] | None  # meta of the last assistant turn
    page_restaurant_id: str | None
    lat: float | None
    lng: float | None
    # Built along the way.
    understanding: Understanding
    budget_pkr: float | None
    max_each_pkr: float | None
    meal: bool
    notes: list[str]  # facts the explanation must mention (conversions, relaxed filters)
    candidates: dict[str, dict[str, Any]]  # item ID → data, as shown to pick
    restaurant_candidates: dict[str, dict[str, Any]]
    profile: str | None
    pick: Pick | None
    feedback: list[str]
    attempts: int
    made_up: int
    rejections: list[dict[str, Any]]  # why the check turned picks down: [{attempt, problems}]
    # Out.
    dishes: list[DishCard]
    restaurants: list[RestaurantCard]
    status: TurnStatus
    text: str
    degraded_reason: str | None


def _emit(event: dict[str, Any]) -> None:
    get_stream_writer()(event)


def _status(stage: str) -> None:
    _emit({"type": "status", "stage": stage})


def _system(step: str) -> SystemMessage:
    return SystemMessage(f"{load('assistant_rules').text}\n\n{load(step).text}")


def _rs(amount: float) -> str:
    return checks.rs(amount)


# --- understand ---


def _conversation_block(state: TurnState) -> str:
    lines = []
    if state.get("summary"):
        lines.append(f"Earlier: {state['summary']}")
    for turn in state.get("history", []):
        lines.append(f"{turn['role']}: {turn['content']}")
    prev = state.get("previous")
    if prev:
        lines.append(
            f"Last search: {prev.get('query') or '(none)'}; "
            f"constraints then: {prev.get('constraints', {})}. Last answer showed: "
            + "; ".join(prev.get("shown", []) or ["nothing"])
        )
    return fence("conversation", "\n".join(lines)) if lines else ""


def _understand_messages(state: TurnState) -> list[BaseMessage]:
    page = (
        "The customer is looking at one restaurant's menu."
        if state.get("page_restaurant_id")
        else "The customer is not on a restaurant page."
    )
    parts = [p for p in (_conversation_block(state), page) if p]
    parts.append(fence("customer_message", state["message"]))
    return [SystemMessage(load("understand").text), HumanMessage("\n\n".join(parts))]


def _fallback_understanding(message: str) -> Understanding:
    return Understanding(intent="recommend_dishes", search_query=message[:120])


def _prices(u: Understanding) -> tuple[float | None, float | None, list[str]]:
    """Budget and per-dish cap in PKR; a note for each dollar conversion."""
    notes = []

    c = u.constraints

    def pkr(amount: float | None) -> float | None:
        if amount is None:
            return None
        if c.currency == "USD":
            converted = amount * settings.usd_to_pkr
            notes.append(
                f"${amount:g} is about {_rs(converted)} "
                f"(at a fixed {_rs(settings.usd_to_pkr)} to the dollar)"
            )
            return converted
        return float(amount)

    return pkr(c.budget_total), pkr(c.max_price_each), notes


async def understand(state: TurnState, runtime: Runtime[Deps]) -> dict[str, Any]:
    _status("understanding")
    deps = runtime.context
    degraded: str | None = None
    if not deps.llm.configured:
        u, degraded = _fallback_understanding(state["message"]), "llm_not_configured"
    else:
        try:
            u = await deps.llm.structured("fast", Understanding).ainvoke(
                _understand_messages(state)
            )
        except LLMError as exc:
            logger.warning("understand failed: %s", exc.code)
            u, degraded = _fallback_understanding(state["message"]), exc.code
    u = u.cleaned()
    budget, max_each, notes = _prices(u)
    c = u.constraints
    metrics.ASSISTANT_INTENTS.labels(u.intent).inc()
    return {
        "understanding": u,
        "budget_pkr": budget,
        "max_each_pkr": max_each,
        "meal": budget is not None or (c.people or 1) > 1,
        "notes": notes,
        "degraded_reason": degraded,
        "attempts": 0,
        "feedback": [],
        "made_up": 0,
    }


def route(state: TurnState) -> str:
    intent = state["understanding"].intent
    if intent in CANNED:
        return "reply"
    return "retrieve"


async def reply(state: TurnState) -> dict[str, Any]:
    """Greeting, out of scope, order help (until step 4 wires order
    explanations in): fixed words, no model, no search."""
    intent = state["understanding"].intent
    text = CANNED[intent]
    _emit({"type": "token", "text": text})
    status: TurnStatus = "refused" if intent == "out_of_scope" else "answered"
    return {"text": text, "status": status, "dishes": [], "restaurants": []}


# --- retrieve ---


def filters_for(state: TurnState, *, limit: int) -> Filters:
    u = state["understanding"]
    c: Constraints = u.constraints
    caps = [p for p in (state.get("max_each_pkr"), state.get("budget_pkr")) if p is not None]
    lat, lng = state.get("lat"), state.get("lng")
    here = state.get("page_restaurant_id") if u.this_restaurant else None
    return Filters(
        kind="item",
        lat=lat,
        lng=lng,
        radius_km=settings.assistant_radius_km if lat is not None and lng is not None else None,
        # No single dish may cost more than the whole budget, or the cap.
        max_price=min(caps) if caps else None,
        dietary=tuple(c.dietary),
        restaurant_id=here,
        max_spice=c.max_spice,
        cuisines=tuple(c.cuisines),
        exclude=tuple(c.exclude),
        min_rating=c.min_rating,
        limit=limit,
    )


def _by_id(hits: Sequence[Hit]) -> dict[str, dict[str, Any]]:
    return {h.id: h.data for h in hits}


async def retrieve(state: TurnState, runtime: Runtime[Deps]) -> dict[str, Any]:
    _status("searching")
    deps, u = runtime.context, state["understanding"]
    r = deps.retriever
    query = u.search_query or state["message"]
    try:
        if u.intent == "suggest_restaurants":
            restaurants, profile = await asyncio.gather(
                r.suggest_restaurants(
                    query, filters_for(state, limit=settings.assistant_restaurants)
                ),
                r.customer_profile(state["user_id"]),
            )
            return {
                "restaurant_candidates": _by_id(restaurants),
                "candidates": {},
                "profile": profile,
            }
        searches: list[Awaitable[Any]] = [
            r.retrieve(query, filters_for(state, limit=settings.assistant_candidates)),
            r.customer_profile(state["user_id"]),
        ]
        if u.intent == "order_history":
            searches.append(r.retrieve(query, Filters(customer_id=state["user_id"], limit=5)))
        found = await asyncio.gather(*searches)
    except (ResponseHandlingException, UnexpectedResponse):
        logger.warning("retrieval failed", exc_info=True)
        return {"candidates": {}, "restaurant_candidates": {}, "degraded_reason": "search_down"}
    dishes, profile = found[0], found[1]
    candidates: dict[str, dict[str, Any]] = {}
    if u.intent == "order_history":
        past_ids = [i for order in found[2] for i in order.data.get("menu_item_ids", [])]
        past = await r.items_by_id(past_ids)
        f = filters_for(state, limit=1)
        candidates.update(_by_id([h for h in past if _fits(h.data, f)]))
    for h in dishes:
        candidates.setdefault(h.id, h.data)
    return {"candidates": candidates, "restaurant_candidates": {}, "profile": profile}


def _fits(data: Mapping[str, Any], f: Filters) -> bool:
    """A past-order dish against the turn's filters (search applied them to
    the rest already)."""
    if f.max_price is not None and float(data.get("price", 0)) > f.max_price:
        return False
    if f.max_spice is not None and int(data.get("spice_level") or 0) > f.max_spice:
        return False
    if not set(f.dietary) <= set(data.get("dietary_tags") or []):
        return False
    return f.restaurant_id is None or data.get("restaurant_id") == f.restaurant_id


def after_retrieve(state: TurnState) -> str:
    if state.get("degraded_reason") == "search_down":
        return "search_down"
    if not state.get("candidates") and not state.get("restaurant_candidates"):
        return "nothing"
    return "pick"


async def search_down(state: TurnState) -> dict[str, Any]:
    _emit({"type": "cards", "dishes": [], "restaurants": [], "notes": []})
    _emit({"type": "token", "text": SEARCH_DOWN})
    return {"text": SEARCH_DOWN, "status": "degraded", "dishes": [], "restaurants": []}


async def nothing(state: TurnState) -> dict[str, Any]:
    """Search found nothing under the filters: say so without a model call."""
    c = state["understanding"].constraints
    said = [
        f"{', '.join(c.dietary)}" if c.dietary else "",
        f"spice up to {c.max_spice}" if c.max_spice is not None else "",
        f"no {', '.join(c.exclude)}" if c.exclude else "",
        f"{', '.join(c.cuisines)} cuisine" if c.cuisines else "",
        f"under {_rs(budget)}" if (budget := state.get("budget_pkr")) else "",
        f"rated {c.min_rating:g}+" if c.min_rating else "",
    ]
    filters = "; ".join(s for s in said if s)
    text = NOTHING_FOUND + (f" ({filters}). Try loosening one of those." if filters else "")
    _emit({"type": "cards", "dishes": [], "restaurants": [], "notes": state.get("notes", [])})
    _emit({"type": "token", "text": text})
    metrics.ASSISTANT_OUTCOMES.labels("nothing_found").inc()
    return {"text": text, "status": "refused", "dishes": [], "restaurants": []}


# --- pick ---


def _codes(state: TurnState) -> dict[str, str]:
    """Short codes for this turn's candidates: D1… for dishes, R1… for
    restaurants → their real IDs. The model answers with a code, not a
    36-character ID it can miscopy (1 in 8 Groq runs changed a character).
    Rebuilt from the same candidates on a retry, so codes stay the same within
    a turn. They mean nothing in the next turn: never store them or put them
    in the history or summary."""
    if state["understanding"].intent == "suggest_restaurants":
        ids, prefix = list(state.get("restaurant_candidates", {})), "R"
    else:
        ids, prefix = list(state.get("candidates", {})), "D"
    return {f"{prefix}{n}": real for n, real in enumerate(ids, 1)}


def _decode(chosen: Pick, codes: Mapping[str, str]) -> Pick:
    """The model's codes → real IDs. Anything that isn't a code is left as
    written, so the check reports it as made up, in the model's own words."""

    def real(ref: str) -> str:
        return codes.get(ref.strip().strip("[]").upper(), ref)

    return chosen.model_copy(
        update={
            "items": [i.model_copy(update={"item_id": real(i.item_id)}) for i in chosen.items],
            "restaurant_ids": [real(r) for r in chosen.restaurant_ids],
        }
    )


def _dish_line(code: str, d: Mapping[str, Any]) -> str:
    tags = ", ".join(d.get("dietary_tags") or []) or "no tags"
    desc = " ".join(str(d.get("description", "")).split())[:180]
    return (
        f"[{code}] {d.get('name')} — {d.get('restaurant_name')} "
        f"({d.get('cuisine') or 'restaurant'}) · "
        f"{_rs(float(d.get('price', 0)))} · spice {d.get('spice_level')} · {tags}\n"
        f"    {desc}"
    )


def _restaurant_line(code: str, d: Mapping[str, Any]) -> str:
    rating = (
        f"rated {d['avg_rating']:.1f} by {d.get('review_count', 0)}"
        if d.get("avg_rating")
        else "no ratings yet"
    )
    busy = " · busy right now" if (d.get("load") or {}).get("busy") else ""
    dishes = "; ".join(
        f"{x.get('name')} {_rs(float(x.get('price', 0)))}" for x in d.get("dishes", [])
    )
    return f"[{code}] {d.get('name')} ({d.get('cuisine')}) · {rating}{busy}\n    matching: {dishes}"


def _ask(state: TurnState) -> str:
    u = state["understanding"]
    c = u.constraints.model_dump(exclude_defaults=True)
    lines = [fence("customer_message", state["message"])]
    if c:
        lines.append(f"Constraints understood: {c}")
    if (budget := state.get("budget_pkr")) is not None:
        lines.append(f"Budget for the whole order: {_rs(budget)}.")
    if (cap := state.get("max_each_pkr")) is not None:
        lines.append(f"Price cap per dish: {_rs(cap)}.")
    if u.constraints.people:
        lines.append(f"For {u.constraints.people} people.")
    return "\n".join(lines)


def _pick_messages(state: TurnState, codes: Mapping[str, str]) -> list[BaseMessage]:
    u = state["understanding"]
    parts = [_ask(state)]
    if u.intent == "suggest_restaurants":
        parts.append("Task: suggest restaurants.")
        rs = state["restaurant_candidates"]
        body = "\n".join(_restaurant_line(code, rs[rid]) for code, rid in codes.items())
        parts.append(fence("restaurant_text", body))
    else:
        parts.append("Task: " + ("a meal (one order)." if state.get("meal") else "options."))
        cs = state["candidates"]
        body = "\n".join(_dish_line(code, cs[iid]) for code, iid in codes.items())
        parts.append(fence("menu_text", body))
    if state.get("profile"):
        parts.append(fence("order_history", str(state["profile"])))
    if state.get("feedback"):
        parts.append(
            "Your previous choice was rejected: " + "; ".join(state["feedback"]) + ". Choose again."
        )
    return [_system("pick"), HumanMessage("\n\n".join(parts))]


async def pick(state: TurnState, runtime: Runtime[Deps]) -> dict[str, Any]:
    deps = runtime.context
    attempts = state.get("attempts", 0) + 1
    if not deps.llm.configured or state.get("degraded_reason"):
        # No model, or understand already failed: don't spend another call.
        return {"pick": None, "attempts": attempts}
    _status("choosing")
    try:
        codes = _codes(state)
        chosen = await deps.llm.structured("smart", Pick).ainvoke(_pick_messages(state, codes))
    except LLMError as exc:
        logger.warning("pick failed: %s", exc.code)
        return {"pick": None, "attempts": attempts, "degraded_reason": exc.code}
    return {"pick": _decode(chosen, codes), "attempts": attempts}


# --- check ---


def _fallback_cards(state: TurnState) -> tuple[list[DishCard], list[RestaurantCard]]:
    if state["understanding"].intent == "suggest_restaurants":
        rs = state.get("restaurant_candidates", {})
        return [], [checks.restaurant_card(k, v) for k, v in list(rs.items())[:3]]
    if state.get("meal"):
        meal = checks.fallback_meal(
            state["candidates"],
            budget_pkr=state.get("budget_pkr"),
            people=state["understanding"].constraints.people or 1,
        )
        return meal, []
    return [checks.dish_card(k, v) for k, v in list(state["candidates"].items())[:3]], []


def _rejected(state: TurnState, problems: list[str]) -> list[dict[str, Any]]:
    """Record why the check turned this pick down (or trimmed it): logged, and
    stored with the turn (`ai_interactions.rejections`). Problems name dishes
    and prices only — no customer text."""
    attempt = state.get("attempts", 0)
    logger.info("pick rejected (attempt %d): %s", attempt, "; ".join(problems))
    return [*state.get("rejections", []), {"attempt": attempt, "problems": problems}]


async def check(state: TurnState, runtime: Runtime[Deps]) -> dict[str, Any]:
    _status("checking")
    deps = runtime.context
    chosen = state.get("pick")
    retry = state.get("attempts", 0) < 2
    if chosen is None:
        dishes, restaurants = _fallback_cards(state)
        return {"dishes": dishes, "restaurants": restaurants, "status": "degraded"}
    if chosen.no_match and not chosen.items and not chosen.restaurant_ids:
        metrics.ASSISTANT_OUTCOMES.labels("no_match").inc()
        return {"dishes": [], "restaurants": [], "status": "refused", "notes": _with_note(state)}

    if state["understanding"].intent == "suggest_restaurants":
        result = checks.check_restaurants(chosen, state["restaurant_candidates"], {})
        made_up = state.get("made_up", 0) + result.made_up
        _count_made_up(result.made_up)
        rejections = (
            _rejected(state, result.problems) if result.problems else state.get("rejections", [])
        )
        if not result.restaurants and retry:
            return {"feedback": result.problems, "made_up": made_up, "rejections": rejections}
        if not result.restaurants:
            _, restaurants = _fallback_cards(state)
            return {
                "dishes": [],
                "restaurants": restaurants,
                "status": "degraded",
                "rejections": rejections,
            }
        return {
            "dishes": [],
            "restaurants": result.restaurants,
            "status": "answered",
            "made_up": made_up,
            "notes": _with_note(state),
            "rejections": rejections,
        }

    by_restaurant: dict[str, list[str]] = {}
    for p in chosen.items:
        data = state["candidates"].get(p.item_id)
        if data is not None:
            by_restaurant.setdefault(str(data.get("restaurant_id")), []).append(p.item_id)
    live, loads = await asyncio.gather(deps.quotes(by_restaurant), deps.loads(list(by_restaurant)))
    meal = bool(state.get("meal"))
    result = checks.check_dishes(
        chosen,
        state["candidates"],
        meal=meal,
        budget_pkr=state.get("budget_pkr"),
        max_each_pkr=state.get("max_each_pkr"),
        live=live,
        loads=loads,
    )
    made_up = state.get("made_up", 0) + result.made_up
    _count_made_up(result.made_up)
    rejections = (
        _rejected(state, result.problems) if result.problems else state.get("rejections", [])
    )
    if result.problems and retry:
        metrics.ASSISTANT_OUTCOMES.labels("retried").inc()
        return {"feedback": result.problems, "made_up": made_up, "rejections": rejections}
    usable = checks.meal_is_valid(result, state.get("budget_pkr")) if meal else bool(result.dishes)
    if not usable:
        metrics.ASSISTANT_OUTCOMES.labels("fallback").inc()
        dishes, _ = _fallback_cards(state)
        return {
            "dishes": dishes,
            "restaurants": [],
            "status": "degraded",
            "made_up": made_up,
            "rejections": rejections,
        }
    notes = _with_note(state)
    if meal:
        notes = [*notes, f"The meal comes to {_rs(result.total)} before delivery."]
    return {
        "dishes": result.dishes,
        "restaurants": [],
        "status": "answered",
        "made_up": made_up,
        "notes": notes,
        "rejections": rejections,
    }


def _with_note(state: TurnState) -> list[str]:
    chosen = state.get("pick")
    notes = list(state.get("notes", []))
    if chosen is not None and chosen.note.strip():
        notes.append(chosen.note.strip()[:200])
    return notes


def _count_made_up(n: int) -> None:
    if n:
        metrics.ASSISTANT_MADE_UP_IDS.inc(n)


def after_check(state: TurnState) -> str:
    return "pick" if "status" not in state or state.get("status") is None else "explain"


# --- explain ---


def _explain_messages(state: TurnState) -> list[BaseMessage]:
    lines = [fence("customer_message", state["message"])]
    if state.get("dishes"):
        lines.append(
            "Cards shown:\n"
            + "\n".join(
                f"- {c.quantity} × {c.name} at {c.restaurant_name}, {_rs(c.price)} each, "
                f"spice {c.spice_level}, tags: {', '.join(c.dietary_tags) or 'none'}"
                f"{' (kitchen busy)' if c.busy else ''}"
                f"{f': {c.reason}' if c.reason else ''}"
                for c in state["dishes"]
            )
        )
    elif state.get("restaurants"):
        lines.append(
            "Restaurant cards shown:\n"
            + "\n".join(
                f"- {r.name} ({r.cuisine}), "
                + (f"rated {r.avg_rating:.1f}" if r.avg_rating else "not rated yet")
                + (", busy right now" if r.busy else "")
                + f"; matching dishes: {', '.join(r.dishes)}"
                for r in state["restaurants"]
            )
        )
    else:
        lines.append("No cards: nothing on the menu fits.")
    if state.get("notes"):
        lines.append("Notes to mention: " + " ".join(state["notes"]))
    return [_system("explain"), HumanMessage("\n\n".join(lines))]


async def explain(state: TurnState, runtime: Runtime[Deps]) -> dict[str, Any]:
    _emit(
        {
            "type": "cards",
            "dishes": [c.model_dump() for c in state.get("dishes", [])],
            "restaurants": [r.model_dump() for r in state.get("restaurants", [])],
            "notes": state.get("notes", []),
        }
    )
    if state.get("status") == "degraded":
        _emit({"type": "token", "text": DEGRADED_TEXT})
        return {"text": DEGRADED_TEXT}
    _status("writing")
    deps = runtime.context
    parts: list[str] = []
    try:
        with map_errors():
            async for chunk in deps.llm.model("fast").astream(_explain_messages(state)):
                if piece := chunk.text:
                    parts.append(piece)
                    _emit({"type": "token", "text": piece})
    except LLMError as exc:
        # Cards are already out; finish the words in code.
        logger.warning("explain failed: %s", exc.code)
        has_cards = bool(state.get("dishes") or state.get("restaurants"))
        tail = (
            " (The rest of this answer couldn't be written.)"
            if parts
            else ("Here's what I found for you." if has_cards else NOTHING_FOUND)
        )
        _emit({"type": "token", "text": tail})
        return {"text": "".join(parts) + tail, "degraded_reason": exc.code}
    return {"text": "".join(parts).strip()}


def build() -> CompiledStateGraph[TurnState, Deps, TurnState, TurnState]:
    g = StateGraph(TurnState, context_schema=Deps)
    g.add_node("understand", understand)
    g.add_node("reply", reply)
    g.add_node("retrieve", retrieve)
    g.add_node("search_down", search_down)
    g.add_node("nothing", nothing)
    g.add_node("pick", pick)
    g.add_node("check", check)
    g.add_node("explain", explain)
    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", route, ["reply", "retrieve"])
    g.add_conditional_edges("retrieve", after_retrieve, ["search_down", "nothing", "pick"])
    g.add_edge("pick", "check")
    g.add_conditional_edges("check", after_check, ["pick", "explain"])
    for last in ("reply", "search_down", "nothing", "explain"):
        g.add_edge(last, END)
    return g.compile()


graph = build()
