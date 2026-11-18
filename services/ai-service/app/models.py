"""ai_db tables (docs/week5-plan.md step 0). Imported by alembic/env.py so
autogenerate sees them.

Conversations and their turns are the assistant's memory; interactions are
the log every answer writes (what was asked of the LLM, by which prompt
version and model, what it cost, how it ended) — the source for AI
analytics (step 6) and for debugging one answer. Content drafts (step 5) and
the outbox (step 6) come with the steps that use them.

Stored text is redacted first (app/privacy.py, B5-D8).
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sfo_common import new_id
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class InteractionKind(StrEnum):
    CHAT = "chat"
    ORDER_EXPLAIN = "order_explain"
    CONTENT = "content"


class InteractionStatus(StrEnum):
    ANSWERED = "answered"  # the LLM answered from the context
    REFUSED = "refused"  # nothing matched / out of scope — said so
    DEGRADED = "degraded"  # LLM unavailable: retrieval-only answer
    INTERRUPTED = "interrupted"  # client disconnected mid-stream
    ERROR = "error"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String, index=True)
    # Older turns are folded into this summary, so a long chat sends the
    # model a bounded history (step 2).
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Turns with seq <= this are in `summary` (step 2, migration 0002).
    summarized_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    turn_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"
    # seq orders the turns; unique, so two requests on one conversation
    # can't both write turn N (the second one fails and is retried).
    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_turn_seq"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String)  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # Assistant turns: what was understood and shown (intent, filters, card
    # IDs), so a follow-up ("something cheaper", "the second one") builds on
    # it without re-reading the text.
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Interaction(Base):
    __tablename__ = "ai_interactions"
    __table_args__ = (Index("ix_ai_interactions_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # InteractionKind
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    intent: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)  # InteractionStatus
    # Which words and which model produced the answer.
    prompt_version: Mapped[str] = mapped_column(String, default="", server_default="")
    provider: Mapped[str] = mapped_column(String, default="", server_default="")
    model: Mapped[str] = mapped_column(String, default="", server_default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Time to the first card/token the customer saw (step 3).
    first_output_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # Tokens and time per step: {"pick": {"calls", "input", "cached",
    # "output", "reasoning", "ms"}, ...} (migration 0003).
    steps: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


__all__ = [
    "Base",
    "Conversation",
    "ConversationTurn",
    "Interaction",
    "InteractionKind",
    "InteractionStatus",
]
