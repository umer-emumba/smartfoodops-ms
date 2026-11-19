"""ai-service's events for analytics (docs/week5-plan.md step 6), added to
the outbox in the same transaction as the row they describe; ai-worker
relays them to `ai-events`. Metadata only: no customer words, no answers
(B5-D8). Keyed by user, so one user's events stay in order."""

from typing import Any

from sfo_common.middleware import get_correlation_id
from sfo_events import EventType
from sfo_events.outbox import new_outbox_row

from app.models import Interaction, OutboxEvent


def interaction_recorded(
    row: Interaction,
    *,
    shown_item_ids: list[str] | None = None,
    shown_restaurant_ids: list[str] | None = None,
    restaurant_id: str | None = None,
) -> OutboxEvent:
    """`row` must have its ID (flushed, or set explicitly)."""
    payload: dict[str, Any] = {
        "interaction_id": row.id,
        "user_id": row.user_id,
        "kind": row.kind,
        "intent": row.intent,
        "status": row.status,
        "conversation_id": row.conversation_id,
        "restaurant_id": restaurant_id,
        "shown_item_ids": shown_item_ids or [],
        "shown_restaurant_ids": shown_restaurant_ids or [],
        "latency_ms": row.latency_ms,
        "first_output_ms": row.first_output_ms,
        "input_tokens": row.input_tokens or 0,
        "output_tokens": row.output_tokens or 0,
        "cost_usd": row.cost_usd or 0,
        "model": row.model or "",
    }
    return new_outbox_row(
        OutboxEvent,
        aggregate_id=row.user_id,
        event_type=EventType.AI_INTERACTION_RECORDED,
        payload=payload,
        correlation_id=get_correlation_id() or None,
    )


def content_decided(
    *, draft_id: str, restaurant_id: str, owner_id: str, kind: str, decision: str
) -> OutboxEvent:
    return new_outbox_row(
        OutboxEvent,
        aggregate_id=owner_id,
        event_type=EventType.CONTENT_DRAFT_DECIDED,
        payload={
            "draft_id": draft_id,
            "restaurant_id": restaurant_id,
            "owner_id": owner_id,
            "kind": kind,
            "decision": decision,
        },
        correlation_id=get_correlation_id() or None,
    )
