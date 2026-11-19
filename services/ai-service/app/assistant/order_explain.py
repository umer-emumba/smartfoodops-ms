"""`GET /v1/ai/orders/{id}/explain` (docs/week5-plan.md step 4): the
order's facts diagnosed in code, worded by the model, number-checked
(app/assistant/order_words.py), streamed and logged as an `order_explain`
interaction.

The order is read by the route before the stream starts (as the customer,
app/order_client.py), so not found / not yours stay plain 404 / 403."""

import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import events, metrics, spans
from app.assistant.graph import Deps
from app.assistant.order_facts import diagnose
from app.assistant.order_words import CORRECT_FALLBACKS, PROMPTS, chunks, facts_event, word
from app.llm import usage_scope
from app.models import Interaction, InteractionKind, InteractionStatus
from app.order_client import OrderRecord
from app.prompts import ACTIVE


async def explain_order(
    *,
    user_id: str,
    record: OrderRecord,
    deps: Deps,
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict[str, Any]]:
    """`status → facts → token… → done`."""
    started = time.perf_counter()
    yield {"type": "status", "stage": "checking_order"}
    order = record.order
    load = None
    if order.get("status") in ("confirmed", "assigned"):
        rid = str(order["restaurant_id"])
        load = (await deps.loads([rid])).get(rid)
    d = diagnose(order, record.delivery, record.payment, load)
    yield facts_event(d)
    first_ms = int((time.perf_counter() - started) * 1000)
    yield {"type": "status", "stage": "writing"}
    # No yield inside the span: a generator may be resumed in another context.
    with spans.tracer.start_as_current_span("order_explain.word") as span:
        span.set_attribute("ai.situation", d.code)
        with usage_scope() as usage:
            text, fallback = await word(d, deps.llm)
        span.set_attribute("ai.wording", fallback or "worded")
    for piece in chunks(text):
        yield {"type": "token", "text": piece}
    latency_ms = int((time.perf_counter() - started) * 1000)
    status = "answered" if fallback in CORRECT_FALLBACKS else "degraded"
    metrics.ASSISTANT_ANSWER.labels("order_explain").observe(latency_ms / 1000)
    async with sessions() as session:
        row = Interaction(
            user_id=user_id,
            kind=InteractionKind.ORDER_EXPLAIN,
            intent="order_help",
            status=InteractionStatus(status),
            prompt_version=",".join(f"{n}@v{ACTIVE[n]}" for n in PROMPTS),
            provider=deps.llm.provider if usage.calls else "",
            model=",".join(usage.models),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=Decimal(str(round(usage.cost_usd, 6))),
            latency_ms=latency_ms,
            first_output_ms=first_ms,
            error_code=fallback,
            steps=usage.steps or None,
        )
        session.add(row)
        await session.flush()
        session.add(events.interaction_recorded(row))
        await session.commit()
        interaction_id = row.id
    yield {
        "type": "done",
        "interaction_id": interaction_id,
        "order_id": str(order.get("id")),
        "situation": d.code,
        "status": status,
        "latency_ms": latency_ms,
        "first_output_ms": first_ms,
    }
