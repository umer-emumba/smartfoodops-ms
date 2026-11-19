"""Order explanations (step 4): the reason decided by rules in code, the
model only wording it, and the number guard keeping its numbers honest."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app import metrics
from app.assistant import graph as g
from app.assistant.order_facts import diagnose
from app.assistant.order_words import invented_numbers, numbers, word
from app.assistant.schemas import Understanding
from app.llm import LLMError
from app.order_client import OrderRecord
from app.retrieval import Load
from pydantic import SecretStr

from tests.assistant_fakes import Live, Orders, chat, deps, llm

NOW = datetime(2026, 11, 19, 14, 0, tzinfo=UTC)  # 19:00 in Pakistan


def _at(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def _order(status: str, **over: Any) -> dict[str, Any]:
    return {
        "id": "o-1",
        "order_number": 1042,
        "restaurant_id": "r-1",
        "status": status,
        "status_reason": None,
        "total_amount": "1700.00",
        "refunded_amount": "0.00",
        "created_at": _at(-30),
        "status_history": [],
        **over,
    }


def _why(order: dict, delivery: dict | None = None, payment: dict | None = None, load=None):
    return diagnose(order, delivery, payment, load, now=NOW)


# --- The rules: one situation per state, with real numbers ---


def test_waiting_for_the_restaurant() -> None:
    d = _why(_order("awaiting_restaurant_acceptance", created_at=_at(-6)))
    assert d.code == "awaiting_restaurant"
    assert "sent to the restaurant 6 minutes ago" in d.facts


def test_preparing_on_time() -> None:
    d = _why(_order("confirmed"), {"ready_at": _at(10)})
    assert d.code == "preparing" and "19:10" in d.text


def test_kitchen_late_says_by_how_much_and_how_busy() -> None:
    d = _why(_order("confirmed"), {"ready_at": _at(-12)}, load=Load(18, 20, True))
    assert d.code == "kitchen_late"
    assert d.facts[0] == "the food was due at 18:48, 12 minutes ago"
    assert "the kitchen is very busy: 18 of 20 orders in progress" in d.facts
    quiet = _why(_order("confirmed"), {"ready_at": _at(-12)}, load=Load(1, 20, True))
    assert not any("kitchen" in f for f in quiet.facts)  # a quiet kitchen isn't a reason


def test_kitchen_late_with_a_rider_already_waiting() -> None:
    d = _why(_order("assigned"), {"ready_at": _at(-12), "pickup_eta": _at(-5)})
    assert d.code == "kitchen_late"  # the kitchen, not the rider, is the hold-up
    assert "a rider is assigned and will collect it once it's ready" in d.facts


def test_food_ready_and_finding_a_rider() -> None:
    d = _why(
        _order("confirmed"),
        {"ready_at": _at(-8), "food_ready_at": _at(-4), "reassignment_count": 1},
    )
    assert d.code == "finding_rider"
    assert "the food has been ready for 4 minutes" in d.facts
    assert any("1 rider(s) couldn't take it" in f for f in d.facts)


def test_rider_assigned_and_rider_late() -> None:
    assert _why(_order("assigned"), {"pickup_eta": _at(5)}).code == "rider_assigned"
    d = _why(_order("assigned"), {"pickup_eta": _at(-7)})
    assert d.code == "rider_late" and "7 minutes late" in d.text


def test_on_the_way_and_running_late() -> None:
    d = _why(_order("picked_up"), {"dropoff_eta": _at(15)})
    assert d.code == "on_the_way" and "arriving around 19:15" in d.text
    d = _why(_order("picked_up"), {"dropoff_eta": _at(-9)})
    assert d.code == "running_late" and "running 9 minutes late" in d.text


def test_delivered_on_time_or_late() -> None:
    on_time = _order("delivered", delivered_at=_at(-1))
    assert _why(on_time, {"dropoff_eta": _at(-1)}).code == "delivered"
    late = _order("delivered", delivered_at=_at(-1))
    d = _why(late, {"dropoff_eta": _at(-21)})
    assert d.code == "delivered_late" and "20 minutes after the estimate" in d.text


def test_the_customer_cancelled_and_was_not_charged() -> None:
    d = _why(_order("cancelled", status_reason="cancelled_by_customer"), None, {"status": "voided"})
    assert d.code == "cancelled"
    assert d.facts == ["you (the customer) cancelled it", "you weren't charged for it"]


def test_a_rejection_keeps_the_restaurants_note_apart() -> None:
    order = _order("rejected", status_reason="restaurant_rejected: closing early today")
    d = _why(order)
    assert d.code == "rejected" and "the restaurant declined the order" in d.facts
    assert d.notes == ["closing early today"]  # free text: fenced for the model


def test_payment_declined() -> None:
    d = _why(
        _order("rejected", status_reason="payment_declined"),
        None,
        {"status": "declined", "failure_reason": "card_expired"},
    )
    assert "the payment was declined" in d.facts and "card_expired" in d.notes


def test_a_typed_cancel_note_is_attributed_from_the_history() -> None:
    order = _order(
        "cancelled",
        status_reason="ordered by mistake",
        status_history=[{"to_status": "cancelled", "actor_role": "customer"}],
    )
    d = _why(order)
    assert "you (the customer) cancelled it" in d.facts and d.notes == ["ordered by mistake"]


def test_a_partial_refund() -> None:
    order = _order(
        "delivered",
        refunded_amount="450.00",
        refunded_at=_at(-60),
        status_history=[{"reason": "refunded 450.00: cold food"}],
    )
    d = _why(order)
    assert d.code == "refunded"
    assert d.facts[0] == "a partial refund of Rs 450 of Rs 1,700"
    assert d.notes == ["cold food"]
    assert "Rs 450" in d.text and "19 Nov" in d.text


# --- The number guard ---


def test_numbers_are_normalised() -> None:
    assert numbers("Rs 1,700 at 19:40, #1042, 450.00 and 12 min.") == {
        "1700",
        "19:40",
        "1042",
        "450",
        "12",
    }


def test_numbers_not_in_the_facts_are_caught() -> None:
    d = _why(_order("confirmed"), {"ready_at": _at(-12)})
    assert invented_numbers("Sorry, order #1042 is 12 minutes late (due 18:48).", d) == set()
    assert invented_numbers("It should arrive by 19:30, in about 25 minutes.", d) == {"19:30", "25"}


async def _word(text: str | None = None, error: Exception | None = None):
    d = _why(_order("confirmed"), {"ready_at": _at(-12)})
    fast = chat(text=text or "")
    if error is not None:
        fast = chat(text="x")
        fast.__dict__["messages"] = iter([])  # no answer: the call fails

        async def boom(*a: Any, **k: Any) -> Any:
            raise error

        object.__setattr__(fast, "ainvoke", boom)
    return d, await word(d, llm(chat(), fast))


async def test_good_wording_is_used() -> None:
    d, (text, why) = await _word(
        "Sorry — order #1042's food was due at 18:48 and is 12 minutes late."
    )
    assert why is None and text.startswith("Sorry")


async def test_an_invented_number_falls_back_to_the_code_text() -> None:
    before = metrics.ORDER_EXPLANATIONS.labels("number_guard")._value.get()
    d, (text, why) = await _word("It's 12 minutes late; expect it by 19:30.")
    assert why == "number_guard" and text == d.text
    assert metrics.ORDER_EXPLANATIONS.labels("number_guard")._value.get() == before + 1


async def test_a_failed_model_call_falls_back_to_the_code_text() -> None:
    d, (text, why) = await _word(error=LLMError("llm_unavailable", "down"))
    assert text == d.text and why == "llm_unavailable"


async def test_no_model_uses_the_code_text() -> None:
    d = _why(_order("picked_up"), {"dropoff_eta": _at(15)})
    unconfigured = llm(chat(), chat(), llm_api_key=SecretStr(""))
    unconfigured._factory = None
    assert await word(d, unconfigured) == (d.text, "llm_not_configured")


# --- In the chat: "where is my order?" ---


def _live_at(minutes: float) -> str:
    """The graph diagnoses against the real clock."""
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _record(**over: Any) -> OrderRecord:
    return OrderRecord(_order("picked_up", **over), {"dropoff_eta": _live_at(15)}, None)


async def _ask(d: Any, token: str | None = "secret-tok-7f3a") -> tuple[dict, list[dict]]:
    events: list[dict] = []
    final: dict = {}
    start = {
        "user_id": "u-1",
        "message": "where is my order?",
        "history": [],
        "summary": "",
        "auth_token": token,
    }
    async for mode, chunk in g.graph.astream(start, context=d, stream_mode=["custom", "values"]):
        if mode == "values":
            final = chunk
        else:
            events.append(chunk)
    return final, events


def _help() -> Any:
    return chat(
        Understanding(intent="order_help", search_query="order status"),
        text="Your order #1042 is on the way.",
    )


async def test_order_help_explains_the_latest_order_as_the_customer() -> None:
    orders = Orders(_record())
    final, events = await _ask(deps(chat(), _help(), orders=orders))
    facts = next(e for e in events if e["type"] == "facts")
    assert facts["situation"] == "on_the_way" and facts["order_number"] == 1042
    assert final["text"] == "Your order #1042 is on the way." and final["status"] == "answered"
    assert orders.tokens == ["secret-tok-7f3a"] * 2  # read as the customer, with their token
    assert "secret-tok-7f3a" not in str(events)  # never shown


async def test_order_help_with_no_orders() -> None:
    final, _ = await _ask(deps(chat(), _help(), orders=Orders()))
    assert final["text"] == g.NO_ORDERS


async def test_order_help_without_a_token_is_the_fixed_reply() -> None:
    final, _ = await _ask(deps(chat(), _help()), token=None)
    assert final["text"] == g.CANNED["order_help"]


async def test_order_help_when_orders_cant_be_read() -> None:
    final, _ = await _ask(deps(chat(), _help(), orders=Orders(latest="o-missing")))
    assert final["status"] == "degraded" and final["text"] == g.CANNED["order_help"]


@pytest.mark.parametrize("status", ["confirmed"])
async def test_kitchen_load_is_read_for_a_confirmed_order(status: str) -> None:
    record = OrderRecord(_order(status), {"ready_at": _live_at(-12)}, None)
    live = Live(loads={"r-1": Load(18, 20, True)})
    final, events = await _ask(deps(chat(), _help(), live=live, orders=Orders(record)))
    facts = next(e for e in events if e["type"] == "facts")
    assert facts["situation"] == "kitchen_late"
    assert any("18 of 20" in f for f in facts["facts"])
