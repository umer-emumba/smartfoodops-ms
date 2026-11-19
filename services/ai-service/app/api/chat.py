"""`POST /v1/ai/chat` — the assistant, streamed as Server-Sent Events
(docs/week5-plan.md step 3).

Errors known before the answer starts are plain HTTP: 401 / 403 (not a
customer), 422 (bad body), 404 (unknown or someone else's conversation),
409 (that conversation is still answering). Once the stream is open:

    event: status  {"type": "status", "stage": "understanding"}   (…more)
    event: cards   {"type": "cards", "dishes": [...], "restaurants": [...], "notes": [...]}
    event: token   {"type": "token", "text": "Try the"}            (…more)
    event: done    {"type": "done", "conversation_id": ..., "status": ...}
or, if something breaks mid-answer, a last
    event: error   {"type": "error", "code": "internal", "message": ...}

`: heartbeat` comments keep a slow answer's connection alive through the
gateway (45 s idle limit). A client that disconnects cancels the turn —
and with it the LLM call — and the turn is saved as `interrupted`.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sfo_auth import CurrentUser, Role
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import sse
from app.assistant.deps import default_deps
from app.assistant.graph import Deps
from app.assistant.service import ChatRequest, Turn, maybe_summarize, start_turn, stream_turn
from app.auth_deps import require_roles
from app.config import settings
from app.db import SessionFactory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ai", tags=["ai"])

# Turns finishing their save after a disconnect, and summaries run after the
# stream has closed: kept here so they aren't garbage-collected mid-flight.
_background: set[asyncio.Task[None]] = set()


def _keep(task: asyncio.Task[None]) -> None:
    _background.add(task)
    task.add_done_callback(_background.discard)


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=settings.assistant_max_message_chars)
    conversation_id: str | None = Field(
        default=None, max_length=64, description="Continue this conversation; omit to start one"
    )
    restaurant_id: str | None = Field(
        default=None, max_length=64, description="The restaurant on screen, if any"
    )
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message is empty")
        return v


def get_deps() -> Deps:
    return default_deps()


def get_sessions() -> async_sessionmaker[AsyncSession]:
    return SessionFactory


@router.post("/chat", response_class=StreamingResponse)
async def chat(
    body: ChatBody,
    current: CurrentUser = Depends(require_roles(Role.CUSTOMER)),
    deps: Deps = Depends(get_deps),
    sessions: async_sessionmaker[AsyncSession] = Depends(get_sessions),
) -> StreamingResponse:
    """Ask the food assistant. The caller is always the customer in the
    token; a conversation belongs to whoever started it."""
    turn = await start_turn(
        ChatRequest(
            user_id=current.id,
            message=body.message,
            conversation_id=body.conversation_id,
            restaurant_id=body.restaurant_id,
            lat=body.lat if body.lng is not None else None,
            lng=body.lng if body.lat is not None else None,
        ),
        sessions,
    )
    return StreamingResponse(
        events(turn, deps, sessions), media_type="text/event-stream", headers=sse.HEADERS
    )


async def events(
    turn: Turn,
    deps: Deps,
    sessions: async_sessionmaker[AsyncSession],
    heartbeat_seconds: float | None = None,
) -> AsyncIterator[str]:
    """The turn's events as SSE text, with a heartbeat whenever the turn is
    quiet. The turn runs in its own task, so a heartbeat never interrupts
    it; when this generator stops early (client gone), that task is
    cancelled and saves the turn as `interrupted`."""
    beat = heartbeat_seconds or settings.assistant_heartbeat_seconds
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for event in stream_turn(turn, deps, sessions):
                await queue.put(event)
        finally:
            await queue.put(None)

    task = asyncio.create_task(produce())
    finished = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=beat)
            except TimeoutError:
                yield sse.HEARTBEAT
                continue
            if event is None:
                finished = True
                break
            yield sse.event(event["type"], event)
    finally:
        if not task.done():
            # Client gone: cancel the turn (and its LLM call). The task saves
            # the turn as `interrupted` on its way out; it's kept referenced
            # and waited for — shielded, since a cancelled request may cancel
            # this await too, and the save must still finish.
            task.cancel()
            _keep(task)
            with contextlib.suppress(BaseException):
                await asyncio.shield(task)
    if finished and deps.llm.configured:
        _keep(asyncio.create_task(maybe_summarize(sessions, deps, turn.conversation_id)))
