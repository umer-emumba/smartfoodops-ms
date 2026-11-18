"""One assistant turn end to end against a real ai_db (fake models and
retrieval): memory across turns, whose conversation it is, the interaction
row, redaction, and the summary folding older turns."""

from decimal import Decimal

import pytest
from app.assistant import service
from app.assistant.schemas import Pick, PickedItem, Understanding
from app.assistant.service import ChatRequest, run_turn
from app.models import Conversation, ConversationTurn, Interaction
from langchain_core.messages import AIMessage
from sfo_common.errors import NotFoundError
from sqlalchemy import select

from tests.assistant_fakes import StreamsUsage, chat, deps

pytestmark = pytest.mark.db


def _u(**over) -> Understanding:
    return Understanding(intent="recommend_dishes", search_query="curry", **over)


def _pick(item_id: str = "m-1") -> Pick:
    return Pick(items=[PickedItem(item_id=item_id, reason="mild")])


async def _turn(sessions, d, message: str, conversation_id: str | None = None, user="u-1"):
    events = [
        e
        async for e in run_turn(
            ChatRequest(user_id=user, message=message, conversation_id=conversation_id),
            d,
            sessions,
        )
    ]
    return events[-1], events


async def test_a_turn_is_stored_with_its_interaction(sessions) -> None:
    fast = StreamsUsage(
        messages=iter([]),
        answers={"Understanding": [_u()]},
        asked=[],
        text="Try the qorma.",
        usage={"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050},
        model_name="claude-haiku-5-5",
    )
    done, events = await _turn(sessions, deps(chat(_pick()), fast), "mild curry, call 03001234567")

    assert done["type"] == "done" and done["status"] == "answered"
    assert done["intent"] == "recommend_dishes"
    assert done["first_output_ms"] is not None and done["latency_ms"] >= done["first_output_ms"]
    async with sessions() as s:
        turns = (await s.scalars(select(ConversationTurn).order_by(ConversationTurn.seq))).all()
        row = await s.get(Interaction, done["interaction_id"])
    assert [(t.seq, t.role) for t in turns] == [(1, "user"), (2, "assistant")]
    assert turns[0].content == "mild curry, call [phone]"  # redacted before storing
    assert turns[1].content == "Try the qorma."
    assert turns[1].meta["item_ids"] == ["m-1"] and turns[1].meta["intent"] == "recommend_dishes"
    assert row.kind == "chat" and row.status == "answered"
    assert row.prompt_version == "assistant_rules@v1,understand@v2,pick@v2,explain@v1"
    # The explain call's usage (the fakes' structured calls report none).
    assert (row.input_tokens, row.output_tokens) == (1000, 50)
    assert row.cost_usd == Decimal("0.000125")  # 1000 x $0.10 + 50 x $0.50, per million
    # Per step, named after the graph node that made the call.
    assert row.steps is not None and set(row.steps) == {"explain"}
    assert {k: v for k, v in row.steps["explain"].items() if k != "ms"} == {
        "calls": 1,
        "input": 1000,
        "cached": 0,
        "output": 50,
        "reasoning": 0,
    }


async def test_a_follow_up_sees_the_previous_turn(sessions) -> None:
    first, _ = await _turn(sessions, deps(chat(_pick()), chat(_u())), "mild curry")
    fast = chat(_u(refers_to_previous=True))
    second, _ = await _turn(
        sessions, deps(chat(_pick("m-2")), fast), "something cheaper", first["conversation_id"]
    )
    assert second["conversation_id"] == first["conversation_id"]
    asked = fast.asked[0][1][-1].content
    assert "<conversation>" in asked and "user: mild curry" in asked
    assert "Last search: curry" in asked
    assert "Last answer showed: 1 × Chicken Qorma (Desi Dastarkhwan, Rs 900)" in asked


async def test_someone_elses_conversation_is_not_found(sessions) -> None:
    first, _ = await _turn(sessions, deps(chat(_pick()), chat(_u())), "curry")
    with pytest.raises(NotFoundError):
        await _turn(
            sessions, deps(chat(), chat()), "hi", first["conversation_id"], user="u-intruder"
        )


async def test_degraded_turns_are_logged_as_such(sessions) -> None:
    from pydantic import SecretStr

    d = deps(chat(), chat(), llm_api_key=SecretStr(""))
    d.llm._factory = None
    done, _ = await _turn(sessions, d, "curry")
    async with sessions() as s:
        row = await s.get(Interaction, done["interaction_id"])
    assert done["status"] == "degraded"
    assert (row.status, row.error_code, row.input_tokens, row.provider) == (
        "degraded",
        "llm_not_configured",
        0,
        "",
    )


async def test_older_turns_are_folded_into_the_summary(sessions, monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "assistant_summarize_after", 4)
    monkeypatch.setattr(service.settings, "assistant_history_turns", 2)
    conversation_id = None
    for n in range(3):  # 6 turns stored
        fast = chat(_u(), text=f"answer {n}")
        fast.messages = iter([AIMessage(f"answer {n}"), AIMessage("Wants mild curries.")])
        done, _ = await _turn(sessions, deps(chat(_pick()), fast), f"question {n}", conversation_id)
        conversation_id = done["conversation_id"]
    async with sessions() as s:
        conversation = await s.get(Conversation, conversation_id)
    assert conversation.turn_count == 6
    assert conversation.summarized_seq == 4 and conversation.summary == "Wants mild curries."
