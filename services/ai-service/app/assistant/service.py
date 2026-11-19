"""One assistant turn end to end (docs/week5-plan.md steps 2–3): claim the
conversation and load its memory (`start_turn`, before any byte is
streamed, so a busy or unknown conversation is a plain 409 / 404), run the
graph and stream its events (`stream_turn`), then store both turns and the
interaction row — also when the client goes away mid-answer
(`interrupted`) or something breaks (`error`).

One turn at a time per conversation: `busy_until` is set atomically when a
turn starts and cleared when it is saved; it expires on its own if the
process dies.

Memory (B5-D8: stored text is redacted): the last
`assistant_history_turns` turns go to the model word for word, older ones
as the conversation's summary. The summary is updated after the answer has
been sent — the customer never waits for it.

Events, in order: `status`… → `cards` → `token`… → `done`. `done` carries
the conversation and interaction IDs, the status and the timings."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from sfo_common.errors import ConflictError, NotFoundError
from sqlalchemy import func, or_, select, update
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


BUSY = "Still answering your last message in this conversation."


async def _claim(session: AsyncSession, req: ChatRequest) -> Memory:
    """Marks the conversation busy and loads its memory. One UPDATE … WHERE
    not busy, so two messages arriving together can't both get through, on
    any number of replicas. Database time throughout."""
    until = func.now() + timedelta(seconds=settings.assistant_turn_lock_seconds)
    if req.conversation_id is None:
        conversation = Conversation(user_id=req.user_id, busy_until=until)
        session.add(conversation)
        await session.flush()
        return Memory(conversation, [], None)
    claimed = await session.execute(
        update(Conversation)
        .where(
            Conversation.id == req.conversation_id,
            Conversation.user_id == req.user_id,
            or_(Conversation.busy_until.is_(None), Conversation.busy_until < func.now()),
        )
        .values(busy_until=until)
        .returning(Conversation.id)
    )
    if claimed.first() is None:
        found = await session.get(Conversation, req.conversation_id)
        if found is None or found.user_id != req.user_id:
            # Someone else's conversation looks the same as none at all.
            raise NotFoundError("Conversation not found")
        raise ConflictError(BUSY)
    loaded = await session.get(Conversation, req.conversation_id, populate_existing=True)
    assert loaded is not None
    conversation = loaded
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


@dataclass(frozen=True)
class Turn:
    """A claimed turn, ready to stream."""

    req: ChatRequest
    message: str  # trimmed and redacted
    conversation_id: str
    state: TurnState
    started: float


async def start_turn(req: ChatRequest, sessions: async_sessionmaker[AsyncSession]) -> Turn:
    """Before any event: NotFoundError for an unknown or someone else's
    conversation, ConflictError while it is still answering."""
    started = time.perf_counter()
    message = redact(" ".join(req.message.split())[: settings.assistant_max_message_chars])
    async with sessions() as session:
        memory = await _claim(session, req)
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
    return Turn(req, message, conversation_id, state, started)


ERROR_TEXT = "Something went wrong on our side. Please try again."


async def stream_turn(
    turn: Turn, deps: Deps, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[dict[str, Any]]:
    """The graph's events, then `done` — or a final `error` event. Always
    saves the turn and frees the conversation: answered / refused /
    degraded, `interrupted` (client gone: cancelled or closed; what was
    streamed so far is kept) or `error`."""
    final: dict[str, Any] = {}
    first_output_ms: int | None = None
    text: list[str] = []
    saved = False

    def elapsed_ms() -> int:
        return int((time.perf_counter() - turn.started) * 1000)

    async def save(status: str, **over: Any) -> str:
        nonlocal saved
        interaction_id = await _save(
            sessions,
            conversation_id=turn.conversation_id,
            req=turn.req,
            message=turn.message,
            final=final | over,
            status=status,
            usage=usage,
            provider=deps.llm.provider if usage.calls else "",
            latency_ms=elapsed_ms(),
            first_output_ms=first_output_ms,
        )
        saved = True
        metrics.ASSISTANT_TURNS.labels(status).inc()
        return interaction_id

    with usage_scope() as usage:
        try:
            async for mode, chunk in graph.astream(
                turn.state, context=deps, stream_mode=["custom", "values"]
            ):
                data = cast(dict[str, Any], chunk)
                if mode == "values":
                    final = data
                    continue
                if first_output_ms is None and data.get("type") in ("cards", "token"):
                    first_output_ms = elapsed_ms()
                    metrics.ASSISTANT_FIRST_OUTPUT.observe(first_output_ms / 1000)
                if data.get("type") == "token":
                    text.append(data["text"])
                yield data

            status = final.get("status") or "answered"
            if final.get("degraded_reason") and status == "answered":
                status = "degraded"
            interaction_id = await save(status)
        except (asyncio.CancelledError, GeneratorExit):
            # The client went away: the LLM call was cancelled with the graph.
            # Keep what they saw. Shielded: the save must outlive the cancel.
            logger.info("assistant turn interrupted after %d ms", elapsed_ms())
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    save("interrupted", text="".join(text), degraded_reason="client_gone")
                )
            raise
        except Exception:
            logger.exception("assistant turn failed")
            with contextlib.suppress(Exception):
                await save("error", text="".join(text), degraded_reason="internal")
            yield {"type": "error", "code": "internal", "message": ERROR_TEXT}
            return
        finally:
            if not saved:
                await _release(sessions, turn.conversation_id)

    yield {
        "type": "done",
        "conversation_id": turn.conversation_id,
        "interaction_id": interaction_id,
        "status": status,
        "intent": final["understanding"].intent if final.get("understanding") else None,
        "latency_ms": elapsed_ms(),
        "first_output_ms": first_output_ms,
    }


async def run_turn(
    req: ChatRequest, deps: Deps, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[dict[str, Any]]:
    """start + stream + summary, in one: the CLI and the eval. The SSE route
    (app/api/chat.py) uses the parts, so the summary runs after the stream
    has closed."""
    turn = await start_turn(req, sessions)
    async for event in stream_turn(turn, deps, sessions):
        yield event
    if deps.llm.configured:
        await maybe_summarize(sessions, deps, turn.conversation_id)


async def _release(sessions: async_sessionmaker[AsyncSession], conversation_id: str) -> None:
    """Frees a conversation whose turn couldn't be saved. Best effort: the
    mark expires anyway."""
    try:
        async with sessions() as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(busy_until=None)
            )
            await session.commit()
    except Exception:
        logger.warning("couldn't free conversation %s; it frees itself", conversation_id)


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
        conversation.busy_until = None  # the next message may come in
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
            rejections=final.get("rejections") or None,
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
