"""One assistant turn end to end (docs/week5-plan.md step 2): load the
conversation's memory, run the graph, stream its events, then store both
turns and the interaction row. Step 3's SSE route is a thin wrapper around
`run_turn`.

Memory (B5-D8: stored text is redacted): the last
`assistant_history_turns` turns go to the model word for word, older ones
as the conversation's summary. The summary is updated after the answer has
been sent — the customer never waits for it.

Events, in order: `status`… → `cards` → `token`… → `done`. `done` carries
the conversation and interaction IDs, the status and the timings."""

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from sfo_common.errors import NotFoundError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import metrics
from app.assistant.graph import Deps, TurnState, graph
from app.config import settings
from app.llm import LLMError, map_errors, usage_scope
from app.models import (
    Conversation,
    ConversationTurn,
    Interaction,
    InteractionKind,
    InteractionStatus,
)
from app.privacy import redact
from app.prompts import ACTIVE, fence, load

logger = logging.getLogger(__name__)

# Logged with every chat interaction: the exact prompt words behind it.
PROMPTS = ("assistant_rules", "understand", "pick", "explain")


def prompt_version() -> str:
    return ",".join(f"{name}@v{ACTIVE[name]}" for name in PROMPTS)


@dataclass(frozen=True)
class ChatRequest:
    user_id: str
    message: str
    conversation_id: str | None = None
    # Page context: the restaurant on screen; the delivery location, resolved
    # by the app — coordinates only (B5-D8).
    restaurant_id: str | None = None
    lat: float | None = None
    lng: float | None = None


@dataclass
class Memory:
    conversation: Conversation
    history: list[dict[str, str]]
    previous: dict[str, Any] | None


async def _memory(session: AsyncSession, req: ChatRequest) -> Memory:
    if req.conversation_id is None:
        conversation = Conversation(user_id=req.user_id)
        session.add(conversation)
        await session.flush()
        return Memory(conversation, [], None)
    found = await session.get(Conversation, req.conversation_id)
    if found is None or found.user_id != req.user_id:
        # Someone else's conversation looks the same as none at all.
        raise NotFoundError("Conversation not found")
    conversation = found
    turns = (
        await session.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.seq > conversation.summarized_seq,
            )
            .order_by(ConversationTurn.seq.desc())
            .limit(settings.assistant_history_turns)
        )
    ).all()
    turns = list(reversed(turns))
    previous = next((t.meta for t in reversed(turns) if t.role == "assistant"), None)
    return Memory(
        conversation,
        [{"role": t.role, "content": t.content} for t in turns],
        previous,
    )


def _meta(state: TurnState) -> dict[str, Any]:
    """What a follow-up needs from this answer: what was understood and
    what was shown (names and prices: the IDs live in the cards)."""
    u = state.get("understanding")
    shown = [
        f"{c.quantity} × {c.name} ({c.restaurant_name}, Rs {c.price:,.0f})"
        for c in state.get("dishes", [])
    ]
    shown += [f"{r.name} ({r.cuisine})" for r in state.get("restaurants", [])]
    return {
        "intent": u.intent if u else None,
        "query": u.search_query if u else "",
        "constraints": u.constraints.model_dump(exclude_defaults=True) if u else {},
        "shown": shown,
        "item_ids": [c.item_id for c in state.get("dishes", [])],
        "restaurant_ids": [r.restaurant_id for r in state.get("restaurants", [])],
    }


async def run_turn(
    req: ChatRequest, deps: Deps, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[dict[str, Any]]:
    started = time.perf_counter()
    message = redact(" ".join(req.message.split())[: settings.assistant_max_message_chars])
    async with sessions() as session:
        memory = await _memory(session, req)
        await session.commit()
        conversation_id = memory.conversation.id
        summary = memory.conversation.summary

    state: TurnState = {
        "user_id": req.user_id,
        "message": message,
        "history": memory.history,
        "summary": summary,
        "previous": memory.previous,
        "page_restaurant_id": req.restaurant_id,
        "lat": req.lat,
        "lng": req.lng,
    }
    final: dict[str, Any] = {}
    first_output_ms: int | None = None
    with usage_scope() as usage:
        async for mode, chunk in graph.astream(
            state, context=deps, stream_mode=["custom", "values"]
        ):
            data = cast(dict[str, Any], chunk)
            if mode == "values":
                final = data
                continue
            if first_output_ms is None and data.get("type") in ("cards", "token"):
                first_output_ms = int((time.perf_counter() - started) * 1000)
            yield data

    latency_ms = int((time.perf_counter() - started) * 1000)
    status = final.get("status") or "answered"
    if final.get("degraded_reason") and status == "answered":
        status = "degraded"
    interaction_id = await _save(
        sessions,
        conversation_id=conversation_id,
        req=req,
        message=message,
        final=final,
        status=status,
        usage=usage,
        provider=deps.llm.provider if usage.calls else "",
        latency_ms=latency_ms,
        first_output_ms=first_output_ms,
    )
    metrics.ASSISTANT_TURNS.labels(status).inc()
    yield {
        "type": "done",
        "conversation_id": conversation_id,
        "interaction_id": interaction_id,
        "status": status,
        "intent": final["understanding"].intent if final.get("understanding") else None,
        "latency_ms": latency_ms,
        "first_output_ms": first_output_ms,
    }
    if deps.llm.configured:
        await maybe_summarize(sessions, deps, conversation_id)


async def _save(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: str,
    req: ChatRequest,
    message: str,
    final: dict[str, Any],
    status: str,
    usage: Any,
    provider: str,
    latency_ms: int,
    first_output_ms: int | None,
) -> str:
    u = final.get("understanding")
    async with sessions() as session:
        # Row lock: two turns on one conversation get consecutive seqs.
        conversation = await session.get(Conversation, conversation_id, with_for_update=True)
        assert conversation is not None
        seq = conversation.turn_count
        session.add(
            ConversationTurn(
                conversation_id=conversation_id, seq=seq + 1, role="user", content=message
            )
        )
        session.add(
            ConversationTurn(
                conversation_id=conversation_id,
                seq=seq + 2,
                role="assistant",
                content=redact(final.get("text", "")),
                meta=_meta(final),  # type: ignore[arg-type]
            )
        )
        conversation.turn_count = seq + 2
        conversation.updated_at = datetime.now(UTC)
        interaction = Interaction(
            user_id=req.user_id,
            kind=InteractionKind.CHAT,
            conversation_id=conversation_id,
            intent=u.intent if u else None,
            status=InteractionStatus(status),
            prompt_version=prompt_version(),
            provider=provider,
            model=",".join(usage.models),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=Decimal(str(round(usage.cost_usd, 6))),
            latency_ms=latency_ms,
            first_output_ms=first_output_ms,
            error_code=final.get("degraded_reason"),
            steps=usage.steps or None,
        )
        session.add(interaction)
        await session.commit()
        return interaction.id


async def maybe_summarize(
    sessions: async_sessionmaker[AsyncSession], deps: Deps, conversation_id: str
) -> None:
    """Folds the oldest unsummarized turns into the summary once more than
    `assistant_summarize_after` have piled up, keeping the latest
    `assistant_history_turns` word for word. Best effort: a failure leaves
    the turns for next time."""
    try:
        async with sessions() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                return
            pending = conversation.turn_count - conversation.summarized_seq
            if pending <= settings.assistant_summarize_after:
                return
            upto = conversation.turn_count - settings.assistant_history_turns
            turns = (
                await session.scalars(
                    select(ConversationTurn)
                    .where(
                        ConversationTurn.conversation_id == conversation_id,
                        ConversationTurn.seq > conversation.summarized_seq,
                        ConversationTurn.seq <= upto,
                    )
                    .order_by(ConversationTurn.seq)
                )
            ).all()
            text = "\n".join(f"{t.role}: {t.content}" for t in turns)
            ask = f"Existing summary: {conversation.summary or '(none)'}\n\n" + fence(
                "conversation", text
            )
            with map_errors():
                reply = await deps.llm.model("fast").ainvoke(
                    [SystemMessage(load("summarize").text), HumanMessage(ask)]
                )
            conversation.summary = redact(str(reply.text).strip())[:2000]
            conversation.summarized_seq = upto
            await session.commit()
    except LLMError as exc:
        logger.warning("conversation summary skipped: %s", exc.code)
