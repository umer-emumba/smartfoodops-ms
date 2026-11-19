"""The AI metrics the Part B PDF asks for (docs/week5-plan.md step 6),
computed at request time from the read model: `ai_interactions` (one per
chat turn, order explanation and content generation — from ai-service's
AiInteractionRecorded), `ai_content_decisions`, and `order_facts`.

Definitions:
- Usage: interactions by kind and per day; distinct users; conversations.
- Questions asked / answered: chat turns, and those whose status is
  `answered` (not refused, degraded, interrupted or failed).
- Order conversion after AI: of the users shown dishes by the assistant in
  the window, the share who placed an order within 24 h after; plus orders
  placed straight from an assistant answer (tagged `ai_interaction_id`).
- Recommendation acceptance: of the dish cards shown, the share ordered in
  an order tagged with the answer that showed them; also by answer (an
  answer counts as accepted if any of its dishes was ordered from it).
- Response time: mean and p95 of the full answer time and of the time to
  the first card or word, per kind.
- Engagement: turns per conversation, follow-up rate (conversations with
  more than one turn), returning users (AI on 2+ days), interrupted
  answers, and for restaurants content drafts accepted vs rejected.

Rates are None when their denominator is zero. Python over the window's
rows — fine at this scale (as app/queries.py)."""

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.mongo import AI_CONTENT_DECISIONS, AI_INTERACTIONS, ORDER_FACTS
from app.queries import Window

CONVERSION_WINDOW = timedelta(hours=24)


def _rate(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(int(len(ordered) * 0.95) - 1, 0)]


def _mean(values: list[int]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def _timings(rows: list[dict[str, Any]]) -> dict[str, int | None]:
    total = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    first = [r["first_output_ms"] for r in rows if r.get("first_output_ms") is not None]
    return {
        "count": len(rows),
        "mean_ms": _mean(total),
        "p95_ms": _p95(total),
        "first_output_mean_ms": _mean(first),
        "first_output_p95_ms": _p95(first),
    }


async def ai_metrics(db: AsyncDatabase, window: Window) -> dict[str, Any]:
    in_window = {"at": {"$gte": window.start, "$lt": window.end}}
    rows = [r async for r in db[AI_INTERACTIONS].find(in_window)]
    decisions = [d async for d in db[AI_CONTENT_DECISIONS].find(in_window)]
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)
    chat = by_kind["chat"]
    customer_rows = chat + by_kind["order_explain"]

    # Orders that could follow an answer: placed in the window, or up to
    # 24 h after its end.
    orders = [
        o
        async for o in db[ORDER_FACTS].find(
            {"placed_at": {"$gte": window.start, "$lt": window.end + CONVERSION_WINDOW}},
            {"customer_id": 1, "placed_at": 1, "item_ids": 1, "ai_interaction_id": 1},
        )
    ]
    orders_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in orders:
        orders_by_user[o["customer_id"]].append(o)

    # --- Conversion: users shown dishes → ordered within 24 h after ---
    shown = [r for r in chat if r.get("shown_item_ids")]
    first_shown: dict[str, Any] = {}
    for r in sorted(shown, key=lambda r: r["at"]):
        first_shown.setdefault(r["user_id"], r["at"])
    converted = {
        user
        for user, at in first_shown.items()
        if any(at <= o["placed_at"] < at + CONVERSION_WINDOW for o in orders_by_user[user])
    }
    tagged = [o for o in orders if o.get("ai_interaction_id") and o["placed_at"] < window.end]

    # --- Acceptance: cards shown → ordered from that answer ---
    shown_by_id = {r["_id"]: set(r["shown_item_ids"]) for r in shown}
    accepted_items: dict[str, set[str]] = defaultdict(set)
    for o in tagged:
        cards = shown_by_id.get(o["ai_interaction_id"])
        if cards:
            accepted_items[o["ai_interaction_id"]] |= cards & set(o.get("item_ids") or [])
    cards_shown = sum(len(c) for c in shown_by_id.values())
    cards_accepted = sum(len(v) for v in accepted_items.values())
    answers_accepted = sum(1 for v in accepted_items.values() if v)

    # --- Engagement ---
    turns = Counter(r["conversation_id"] for r in chat if r.get("conversation_id"))
    days: dict[str, set[Any]] = defaultdict(set)
    for r in customer_rows:
        days[r["user_id"]].add(r["at"].date())
    users = set(days)
    statuses = Counter(r["status"] for r in chat)
    accepted_drafts = sum(1 for d in decisions if d["decision"] == "accepted")

    return {
        "usage": {
            "chat_turns": len(chat),
            "order_explanations": len(by_kind["order_explain"]),
            "content_generations": len(by_kind["content"]),
            "users": len(users),
            "conversations": len(turns),
            "per_day": [
                {"day": str(day), "interactions": n}
                for day, n in sorted(Counter(r["at"].date() for r in rows).items())
            ],
        },
        "questions": {
            "asked": len(chat),
            "answered": statuses.get("answered", 0),
            "answer_rate": _rate(statuses.get("answered", 0), len(chat)),
            "by_status": dict(sorted(statuses.items())),
            "by_intent": dict(sorted(Counter(r.get("intent") or "unknown" for r in chat).items())),
        },
        "conversion": {
            "users_shown_dishes": len(first_shown),
            "users_who_ordered_within_24h": len(converted),
            "conversion_rate": _rate(len(converted), len(first_shown)),
            "orders_from_ai_answers": len(tagged),
        },
        "recommendations": {
            "answers_with_dishes": len(shown_by_id),
            "dish_cards_shown": cards_shown,
            "dish_cards_ordered": cards_accepted,
            "acceptance_rate": _rate(cards_accepted, cards_shown),
            "answers_accepted": answers_accepted,
            "answer_acceptance_rate": _rate(answers_accepted, len(shown_by_id)),
        },
        "response_time": {kind: _timings(by_kind[kind]) for kind in sorted(by_kind)},
        "engagement": {
            "turns_per_conversation": round(sum(turns.values()) / len(turns), 2) if turns else None,
            "follow_up_rate": _rate(sum(1 for n in turns.values() if n > 1), len(turns)),
            "returning_users": sum(1 for d in days.values() if len(d) > 1),
            "returning_user_rate": _rate(sum(1 for d in days.values() if len(d) > 1), len(users)),
            "interrupted_rate": _rate(statuses.get("interrupted", 0), len(chat)),
            "content_drafts_decided": len(decisions),
            "content_drafts_accepted": accepted_drafts,
            "content_acceptance_rate": _rate(accepted_drafts, len(decisions)),
        },
    }
