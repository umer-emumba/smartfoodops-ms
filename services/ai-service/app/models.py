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
    # Set while a turn is being answered, so a second message on the same
    # conversation gets 409 instead of running without the first answer in
    # its history. Expires on its own if the answering process dies (step 3,
    # migration 0005).
    busy_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # Why the check turned picks down: [{"attempt": 1, "problems": [...]}]
    # (migration 0004). Dish names and prices only.
    rejections: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class ContentKind(StrEnum):
    """What a restaurant can generate (Part B PDF, "Restaurant Productivity
    Enhancement"; docs/week5-plan.md step 5)."""

    MENU_DESCRIPTION = "menu_description"
    PROMOTION = "promotion"
    DISH_PICKS = "dish_picks"
    ENGAGEMENT_MESSAGE = "engagement_message"
    FEEDBACK_SUMMARY = "feedback_summary"


class DraftStatus(StrEnum):
    PENDING = "pending"  # queued for ai-worker
    READY = "ready"  # generated and checked; waiting for the owner
    FAILED = "failed"  # couldn't be generated (error kept)
    ACCEPTED = "accepted"  # the owner took it (and, where it applies, it was applied)
    REJECTED = "rejected"


class ContentDraft(Base):
    """AI-written restaurant content — a draft until its owner accepts it
    (B5-D7): nothing reaches the catalog without that (migration 0006)."""

    __tablename__ = "content_drafts"
    __table_args__ = (Index("ix_content_drafts_restaurant_created", "restaurant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    restaurant_id: Mapped[str] = mapped_column(String)
    requested_by: Mapped[str] = mapped_column(String)  # the owner's user ID
    kind: Mapped[str] = mapped_column(String)  # ContentKind
    # What the owner asked for: menu_item_id, goal, tone, …
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default=DraftStatus.PENDING)
    # The checked result, shaped per kind (app/content/kinds.py).
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    # What accepting created or changed: {"promotion_id": …} / {"menu_item_id": …}
    applied: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    interaction_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


__all__ = [
    "ContentDraft",
    "ContentKind",
    "DraftStatus",
    "Base",
    "Conversation",
    "ConversationTurn",
    "Interaction",
    "InteractionKind",
    "InteractionStatus",
]
