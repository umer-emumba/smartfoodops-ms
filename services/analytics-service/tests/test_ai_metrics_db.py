"""The AI metrics (app/ai_queries.py, week5-plan step 6) over hand-built
events against a real Mongo — every number worked out by hand."""

from typing import Any

import pytest
from app import ai_queries, facts
from app.queries import Window
from pymongo.asynchronous.database import AsyncDatabase
from sfo_events import EventType

from tests.events import at, event

pytestmark = pytest.mark.db

E = EventType
WINDOW = Window(at(0), at(48 * 60))  # two days


def ai(
    interaction_id: str,
    minute: float,
    *,
    user: str = "u-a",
    kind: str = "chat",
    status: str = "answered",
    conversation: str | None = "c-1",
    shown: list[str] | None = None,
    latency: int = 4000,
    first: int | None = 3000,
) -> Any:
    return event(
        E.AI_INTERACTION_RECORDED,
        {
            "interaction_id": interaction_id,
            "user_id": user,
            "kind": kind,
            "intent": "order_help" if kind == "order_explain" else "recommend_dishes",
            "status": status,
            "conversation_id": conversation if kind == "chat" else None,
            "shown_item_ids": shown or [],
            "latency_ms": latency,
            "first_output_ms": first,
            "input_tokens": 5000,
            "output_tokens": 600,
            "cost_usd": "0.002",
        },
        minute,
    )


def placed(order_id: str, minute: float, user: str, items: list[str], tag: str | None) -> Any:
    return event(
        E.ORDER_PLACED,
        {
            "order_id": order_id,
            "order_number": 1,
            "customer_id": user,
            "restaurant_id": "r-1",
            "restaurant_admin_id": "u-owner",
            "total_amount": "900",
            "items": [{"menu_item_id": i, "quantity": 1} for i in items],
            "ai_interaction_id": tag,
        },
        minute,
    )


def decided(draft: str, minute: float, decision: str) -> Any:
    return event(
        E.CONTENT_DRAFT_DECIDED,
        {
            "draft_id": draft,
            "restaurant_id": "r-1",
            "owner_id": "u-owner",
            "kind": "promotion",
            "decision": decision,
        },
        minute,
    )


@pytest.fixture
async def world(db: AsyncDatabase) -> AsyncDatabase:
    """u-a: two chat turns in c-1 (the first shows m-1, m-2, m-3), orders m-1
    and m-9 from it 30 min later; asks again on day 2 (c-2, shows m-4).
    u-b: one turn shows m-5, never orders. u-c: one refused turn, an order
    explanation, and an untagged order 2 h later (no dishes were shown to
    u-c: not a conversion). Content: 2 drafts accepted, 1 rejected. A chat
    turn after the window is ignored."""
    events = [
        ai("ai-1", 0, shown=["m-1", "m-2", "m-3"], latency=5000, first=3000),
        ai("ai-2", 5, latency=3000, first=2000),  # follow-up, no dishes
        placed("o-1", 30, "u-a", ["m-1", "m-9"], tag="ai-1"),
        ai("ai-3", 26 * 60, conversation="c-2", shown=["m-4"]),  # day 2
        ai("ai-4", 10, user="u-b", conversation="c-3", shown=["m-5"], status="answered"),
        ai("ai-5", 20, user="u-c", conversation="c-4", status="refused", first=None),
        ai("ai-6", 21, user="u-c", kind="order_explain", latency=2000, first=1000),
        placed("o-2", 140, "u-c", ["m-7"], tag=None),
        ai("ai-7", 22, user="u-b", conversation="c-3", status="interrupted"),
        ai("ai-8", 60 * 60, user="u-z", conversation="c-9", shown=["m-1"]),  # after
        decided("d-1", 30, "accepted"),
        decided("d-2", 31, "accepted"),
        decided("d-3", 32, "rejected"),
    ]
    for e in events + events[:3]:  # replays change nothing
        await facts.apply_event(db, e)
    return db


async def test_the_six_ai_metrics(world: AsyncDatabase) -> None:
    m = await ai_queries.ai_metrics(world, WINDOW)

    assert m["usage"] == {
        "chat_turns": 6,  # ai-1,2,3,4,5,7 (ai-8 is after the window)
        "order_explanations": 1,
        "content_generations": 0,
        "users": 3,
        "conversations": 4,  # c-1, c-2, c-3, c-4
        "per_day": [
            {"day": "2026-10-15", "interactions": 6},
            {"day": "2026-10-16", "interactions": 1},
        ],
    }
    q = m["questions"]
    assert (q["asked"], q["answered"], q["answer_rate"]) == (6, 4, 0.6667)
    assert q["by_status"] == {"answered": 4, "interrupted": 1, "refused": 1}

    # u-a and u-b were shown dishes; u-a ordered 30 min later. u-c ordered
    # but was never shown dishes.
    assert m["conversion"] == {
        "users_shown_dishes": 2,
        "users_who_ordered_within_24h": 1,
        "conversion_rate": 0.5,
        "orders_from_ai_answers": 1,
    }
    # Cards: ai-1 (3) + ai-3 (1) + ai-4 (1) = 5 shown; m-1 ordered from ai-1
    # (m-9 wasn't shown). 1 of 3 answers with dishes led to an order.
    assert m["recommendations"] == {
        "answers_with_dishes": 3,
        "dish_cards_shown": 5,
        "dish_cards_ordered": 1,
        "acceptance_rate": 0.2,
        "answers_accepted": 1,
        "answer_acceptance_rate": 0.3333,
    }
    chat = m["response_time"]["chat"]
    assert chat["count"] == 6 and chat["mean_ms"] == round((5000 + 3000 + 4000 * 4) / 6)
    assert chat["first_output_mean_ms"] == round((3000 + 2000 + 3000 * 3) / 5)  # ai-5: none
    assert m["response_time"]["order_explain"]["mean_ms"] == 2000

    e = m["engagement"]
    assert e["turns_per_conversation"] == 1.5  # c-1: 2, c-3: 2, c-2: 1, c-4: 1
    assert e["follow_up_rate"] == 0.5
    assert (e["returning_users"], e["returning_user_rate"]) == (1, 0.3333)  # u-a, 2 days
    assert e["interrupted_rate"] == 0.1667
    assert (e["content_drafts_decided"], e["content_drafts_accepted"]) == (3, 2)
    assert e["content_acceptance_rate"] == 0.6667


async def test_no_ai_activity_is_none_not_zero(db: AsyncDatabase) -> None:
    m = await ai_queries.ai_metrics(db, WINDOW)
    assert m["questions"]["answer_rate"] is None
    assert m["recommendations"]["acceptance_rate"] is None
    assert m["engagement"]["turns_per_conversation"] is None


async def test_an_older_order_placed_event_without_the_tag_still_applies(db) -> None:
    """Backward compatible: OrderPlaced from before step 6 has no
    ai_interaction_id."""
    old = placed("o-old", 0, "u-a", ["m-1"], tag=None)
    payload = {k: v for k, v in dict(old.payload).items() if k != "ai_interaction_id"}
    from sfo_events import validate_payload

    old = old.model_copy(update={"payload": validate_payload(E.ORDER_PLACED, payload)})
    await facts.apply_event(db, old)
    doc = await db["order_facts"].find_one({"_id": "o-old"})
    assert doc["ai_interaction_id"] is None and doc["item_ids"] == ["m-1"]
