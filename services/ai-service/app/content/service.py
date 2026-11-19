"""Content drafts end to end (docs/week5-plan.md step 5).

API side: `create` stores a pending draft and queues it; `accept` applies it
— through restaurant-service with the owner's own token (B5-D26 again: the
catalog's own rules and ownership decide) — and `reject` closes it.

Worker side (`generate`, ai-worker): claims a pending draft, reads the
restaurant's facts from the index, asks the model, checks the answer in
code (one retry with the problems), and stores the result as `ready`, or
`failed` with the reason. Transient LLM errors (rate limit, outage,
timeout) leave it pending for the next attempt, up to
`content_max_attempts`. Every generation is an `ai_interactions` row (kind
`content`)."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sfo_common.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import metrics
from app.config import settings
from app.content import data
from app.content.kinds import KINDS, FeedbackBatchOut, batch_messages, batches
from app.llm import LLM, LLMError, LLMNotConfigured, map_errors, usage_scope
from app.models import ContentDraft, DraftStatus, Interaction, InteractionKind, InteractionStatus
from app.prompts import ACTIVE
from app.restaurant_client import client as restaurants

logger = logging.getLogger(__name__)

# Worth another attempt later (app/llm.py's error classes).
TRANSIENT = {"llm_rate_limited", "llm_unavailable"}


def _now() -> datetime:
    return datetime.now(UTC)


def view(d: ContentDraft) -> dict[str, Any]:
    return {
        "id": d.id,
        "restaurant_id": d.restaurant_id,
        "kind": d.kind,
        "params": d.params,
        "status": d.status,
        "output": d.output,
        "error": d.error,
        "applied": d.applied,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


# --- Who may ---


async def owner_of(restaurant_id: str) -> str:
    """restaurant-service's internal owner lookup (Redis-cached there)."""
    try:
        resp = await restaurants.get(f"/internal/restaurants/{restaurant_id}/owner")
    except httpx.HTTPError as exc:
        raise ConflictError("Restaurants are unavailable right now; try again") from exc
    if resp.status_code == 404:
        raise NotFoundError("Restaurant not found")
    resp.raise_for_status()
    return str(resp.json()["admin_user_id"])


async def ensure_owner(restaurant_id: str, user_id: str) -> None:
    if await owner_of(restaurant_id) != user_id:
        raise ForbiddenError("Not your restaurant")


# --- API side ---


async def create(
    sessions: async_sessionmaker[AsyncSession],
    *,
    restaurant_id: str,
    user_id: str,
    kind: str,
    params: dict[str, Any],
    enqueue: Any,
) -> ContentDraft:
    async with sessions() as session:
        draft = ContentDraft(
            restaurant_id=restaurant_id,
            requested_by=user_id,
            kind=kind,
            params=params,
            status=DraftStatus.PENDING,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(draft)
        await session.commit()
    metrics.CONTENT_DRAFTS.labels(kind, "requested").inc()
    # After the commit: the sweep re-queues it if this publish is lost.
    try:
        await asyncio.to_thread(enqueue, draft.id)
    except Exception:
        logger.warning("couldn't queue draft %s; the sweep will", draft.id, exc_info=True)
    return draft


async def get_owned(
    sessions: async_sessionmaker[AsyncSession], draft_id: str, user_id: str
) -> ContentDraft:
    async with sessions() as session:
        draft = await session.get(ContentDraft, draft_id)
    if draft is None or draft.requested_by != user_id:
        raise NotFoundError("Draft not found")
    return draft


async def list_owned(
    sessions: async_sessionmaker[AsyncSession], restaurant_id: str, limit: int = 50
) -> list[ContentDraft]:
    async with sessions() as session:
        rows = await session.scalars(
            select(ContentDraft)
            .where(ContentDraft.restaurant_id == restaurant_id)
            .order_by(ContentDraft.created_at.desc())
            .limit(limit)
        )
        return list(rows.all())


async def _decide(
    sessions: async_sessionmaker[AsyncSession],
    draft_id: str,
    status: DraftStatus,
    applied: dict[str, Any] | None = None,
) -> ContentDraft:
    """ready → accepted / rejected, once: a second click (or two at once)
    gets 409, not a second promotion."""
    async with sessions() as session:
        result = await session.execute(
            update(ContentDraft)
            .where(ContentDraft.id == draft_id, ContentDraft.status == DraftStatus.READY)
            .values(status=status, applied=applied, updated_at=_now())
            .returning(ContentDraft.id)
        )
        if result.first() is None:
            raise ConflictError("This draft isn't waiting for a decision")
        await session.commit()
        draft = await session.get(ContentDraft, draft_id)
    assert draft is not None
    metrics.CONTENT_DRAFTS.labels(draft.kind, str(status)).inc()
    return draft


async def reject(sessions: async_sessionmaker[AsyncSession], draft: ContentDraft) -> ContentDraft:
    return await _decide(sessions, draft.id, DraftStatus.REJECTED)


async def accept(
    sessions: async_sessionmaker[AsyncSession], draft: ContentDraft, token: str
) -> ContentDraft:
    """Applies what applies (description, promotion) through
    restaurant-service as the owner; the rest are accepted to be used as the
    owner likes (copied into a message, a banner…)."""
    if draft.status != DraftStatus.READY or draft.output is None:
        raise ConflictError("This draft isn't waiting for a decision")
    out, rid = draft.output, draft.restaurant_id
    applied: dict[str, Any] | None = None
    if draft.kind == "menu_description":
        path = f"/v1/restaurants/{rid}/menu/items/{out['menu_item_id']}"
        await _apply("PATCH", path, {"description": out["description"]}, token)
        applied = {"menu_item_id": out["menu_item_id"]}
    elif draft.kind == "promotion":
        body = {
            k: out[k]
            for k in (
                "code",
                "discount_type",
                "discount_value",
                "valid_from",
                "valid_until",
                "max_uses",
            )
        }
        body["discount_value"] = str(body["discount_value"])
        created = await _apply("POST", f"/v1/restaurants/{rid}/promotions", body, token)
        applied = {"promotion_id": created.get("id")}
    return await _decide(sessions, draft.id, DraftStatus.ACCEPTED, applied)


async def _apply(method: str, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    try:
        resp = await restaurants.request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )
    except httpx.HTTPError as exc:
        raise ConflictError("Restaurants are unavailable right now; try again") from exc
    if resp.status_code in (400, 409, 422):
        # restaurant-service's own rules said no (a promo code already in use…).
        detail = resp.json().get("error", {}).get("message", resp.text[:200])
        raise ValidationAppError(f"The restaurant service refused it: {detail}")
    if resp.status_code == 403:
        raise ForbiddenError("Not your restaurant")
    resp.raise_for_status()
    return dict(resp.json()) if resp.content else {}


# --- Worker side ---


async def _claim(sessions: async_sessionmaker[AsyncSession], draft_id: str) -> ContentDraft | None:
    """Pending → one more attempt, counted before the model is called. SKIP
    LOCKED: a duplicate task (sweep, redelivery) for a draft being claimed
    right now does nothing."""
    async with sessions() as session:
        draft = (
            await session.scalars(
                select(ContentDraft)
                .where(ContentDraft.id == draft_id, ContentDraft.status == DraftStatus.PENDING)
                .with_for_update(skip_locked=True)
            )
        ).first()
        if draft is None:
            return None
        draft.attempts += 1
        draft.updated_at = _now()
        await session.commit()
        return draft


async def _ask(llm: LLM, kind: str, facts: data.Facts, params: dict[str, Any]) -> dict[str, Any]:
    spec = KINDS[kind]
    if kind == "feedback_summary":
        if not facts.reviews:
            return {
                "reviews_read": 0,
                "average_rating": None,
                "summary": "No reviews yet.",
                "strengths": [],
                "complaints": [],
                "actions": [],
            }
        notes = []
        with map_errors():
            for batch in batches(facts):
                notes.append(
                    await llm.structured("fast", FeedbackBatchOut).ainvoke(
                        batch_messages(batch, facts)
                    )
                )
        params = {**params, "_batches": notes}
    feedback: list[str] = []
    for _ in range(2):
        with map_errors():
            answer = await llm.structured(spec.role, spec.schema).ainvoke(
                spec.messages(facts, params, feedback)
            )
        output, feedback = spec.check(answer, facts, params)
        if output is not None:
            return output
        logger.info("content draft (%s) rejected by its check: %s", kind, "; ".join(feedback))
        metrics.CONTENT_CHECK_REJECTIONS.labels(kind).inc()
    raise ValueError("the answer didn't pass the checks twice: " + "; ".join(feedback))


async def generate(sessions: async_sessionmaker[AsyncSession], llm: LLM, draft_id: str) -> str:
    """One attempt at a draft. Returns its status afterwards (or "skipped")."""
    draft = await _claim(sessions, draft_id)
    if draft is None:
        return "skipped"
    spec = KINDS[draft.kind]
    started = time.perf_counter()
    status, output, error = DraftStatus.FAILED, None, None
    with usage_scope() as usage:
        try:
            if not llm.configured:
                raise LLMNotConfigured("No LLM is configured")
            facts = await data.facts(
                draft.restaurant_id,
                with_reviews=spec.needs_reviews or draft.kind == "feedback_summary",
                with_orders=spec.needs_orders,
            )
            output = await _ask(llm, draft.kind, facts, dict(draft.params))
            status = DraftStatus.READY
        except LLMError as exc:
            error = exc.code
            if exc.code in TRANSIENT and draft.attempts < settings.content_max_attempts:
                status = DraftStatus.PENDING  # the sweep or a retry tries again
        except (LookupError, ValueError) as exc:
            error = str(exc)[:300]
        except Exception:
            logger.exception("content draft %s failed", draft_id)
            error = "internal"
    await _save(sessions, llm, draft, status, output, error, usage, started)
    metrics.CONTENT_DRAFTS.labels(draft.kind, str(status)).inc()
    logger.info("content draft %s (%s): %s %s", draft_id, draft.kind, status, error or "")
    return str(status)


async def _save(
    sessions: async_sessionmaker[AsyncSession],
    llm: LLM,
    draft: ContentDraft,
    status: DraftStatus,
    output: dict[str, Any] | None,
    error: str | None,
    usage: Any,
    started: float,
) -> str | None:
    spec = KINDS[draft.kind]
    names = ["content_rules", spec.prompt]
    if draft.kind == "feedback_summary":
        names.insert(1, "content_feedback_batch")
    async with sessions() as session:
        # Every attempt is an interaction: ready, failed, or retried later.
        row = Interaction(
            user_id=draft.requested_by,
            kind=InteractionKind.CONTENT,
            intent=draft.kind,
            status=InteractionStatus.ANSWERED
            if status == DraftStatus.READY
            else InteractionStatus.ERROR,
            prompt_version=",".join(f"{n}@v{ACTIVE[n]}" for n in names),
            provider=llm.provider if usage.calls else "",
            model=",".join(usage.models),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=Decimal(str(round(usage.cost_usd, 6))),
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code=error[:60] if error else None,
            steps=usage.steps or None,
        )
        session.add(row)
        await session.flush()
        interaction_id = row.id
        await session.execute(
            update(ContentDraft)
            .where(ContentDraft.id == draft.id)
            .values(
                status=status,
                output=output,
                error=error,
                updated_at=_now(),
                interaction_id=interaction_id,
            )
        )
        await session.commit()
    return interaction_id


async def stale_pending(sessions: async_sessionmaker[AsyncSession], older_than_s: int) -> list[str]:
    cutoff = _now().timestamp() - older_than_s
    async with sessions() as session:
        rows = await session.scalars(
            select(ContentDraft.id)
            .where(ContentDraft.status == DraftStatus.PENDING)
            .where(ContentDraft.updated_at < datetime.fromtimestamp(cutoff, UTC))
            .limit(100)
        )
        return list(rows.all())
