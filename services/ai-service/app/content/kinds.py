"""The five kinds of restaurant content (Part B PDF: menu descriptions,
promotional offers, dish recommendations, customer engagement messages,
feedback summaries).

Same rules as the assistant: the model sees only facts code read
(app/content/data.py); dishes are referred to by short codes (B5-D23); every
number it writes must be in the facts (B5-D27); dates, averages and counts
are worked out in code. Each kind's `check` turns the model's answer into
the stored output or a list of problems (the model gets one retry with
them)."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.assistant.check import rs
from app.assistant.order_words import numbers
from app.assistant.schemas import ModelFacing
from app.config import settings
from app.content.data import Facts, Review
from app.prompts import fence, load

_PKT = timezone(timedelta(hours=settings.display_utc_offset_hours))

# A kind's check: the stored output, or None and the problems.
Checked = tuple[dict[str, Any] | None, list[str]]

# --- What the model returns, per kind ---


class DescriptionOut(ModelFacing):
    description: str = Field(description="1–2 sentences for the menu.")


class PromotionOut(ModelFacing):
    code: str = Field(description="Short code customers type, letters and digits, e.g. LUNCH15.")
    discount_type: Literal["percentage", "fixed_amount"]
    discount_value: float = Field(description="Percent (5–50) or rupees off the order.")
    starts_in_days: int = Field(description="0 = today.")
    lasts_days: int = Field(description="How many days it runs, 1–60.")
    max_uses: int | None = Field(default=None, description="Total uses allowed, or null.")
    pitch: str = Field(description="One line the restaurant can show customers.")


class Pick(ModelFacing):
    code: str = Field(description='The dish code in brackets, e.g. "D3".')
    reason: str


class DishPicksOut(ModelFacing):
    picks: list[Pick] = Field(default_factory=list)


class MessageOut(ModelFacing):
    message: str = Field(description="The message to customers, under 300 characters.")


class FeedbackBatchOut(ModelFacing):
    praise: list[str] = Field(default_factory=list)
    complaints: list[str] = Field(default_factory=list)
    dishes_praised: list[str] = Field(default_factory=list)
    dishes_criticised: list[str] = Field(default_factory=list)


class FeedbackOut(ModelFacing):
    summary: str = Field(description="2–3 sentences.")
    strengths: list[str] = Field(default_factory=list)
    complaints: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list, description="Concrete things to do.")


# --- Facts as text ---


def codes(facts: Facts) -> dict[str, dict[str, Any]]:
    """D1… for this restaurant's dishes (per draft; never stored)."""
    return {f"D{n}": item for n, item in enumerate(facts.menu, 1)}


def _dish_line(code: str, d: dict[str, Any], facts: Facts) -> str:
    tags = ", ".join(d.get("dietary_tags") or []) or "no tags"
    sold = facts.ordered.get(str(d["menu_item_id"]), 0)
    line = (
        f"[{code}] {d['name']} ({d.get('category') or 'dish'}) · {rs(float(d['price']))} · "
        f"spice {d.get('spice_level')} · {tags}"
    )
    if facts.ordered:
        line += f" · ordered {sold} times"
    return line + f"\n    {' '.join(str(d.get('description') or '').split())[:200]}"


def _restaurant_line(facts: Facts) -> str:
    r = facts.restaurant
    avg, n = facts.rating
    rated = f"rated {avg} by {n} reviews" if n else "no reviews yet"
    where = f"{r.get('cuisine') or 'restaurant'}, {r.get('city')}"
    return f"Restaurant: {r.get('name')} — {where}; {rated}."


def _messages(kind: str, body: list[str], feedback: list[str]) -> list[BaseMessage]:
    if feedback:
        body = [*body, "Your previous answer was rejected: " + "; ".join(feedback) + ". Try again."]
    system = f"{load('content_rules').text}\n\n{load(kind).text}"
    return [SystemMessage(system), HumanMessage("\n\n".join(body))]


def _owner_note(params: dict[str, Any], key: str) -> list[str]:
    note = str(params.get(key) or "").strip()
    return [fence("owner_request", note)] if note else []


def _extra_numbers(text: str, allowed_text: str) -> set[str]:
    return numbers(text) - numbers(allowed_text)


# --- One spec per kind ---


@dataclass(frozen=True)
class Kind:
    schema: type[BaseModel]
    prompt: str
    role: Literal["smart", "fast"]
    messages: Callable[[Facts, dict[str, Any], list[str]], list[BaseMessage]]
    check: Callable[[Any, Facts, dict[str, Any]], tuple[dict[str, Any] | None, list[str]]]
    needs_reviews: bool = False
    needs_orders: bool = False


# Menu description ---------------------------------------------------------

_SPICY = re.compile(r"\b(spicy|fiery|hot)\b", re.I)
_MILD = re.compile(r"\b(mild|not spicy|no spice)\b", re.I)
_DIETS = {
    "vegetarian": re.compile(r"\bvegetarian\b", re.I),
    "vegan": re.compile(r"\bvegan\b", re.I),
    "gluten_free": re.compile(r"\bgluten[- ]free\b", re.I),
    "dairy_free": re.compile(r"\bdairy[- ]free\b", re.I),
    "nut_free": re.compile(r"\bnut[- ]free\b", re.I),
}


def _item(facts: Facts, params: dict[str, Any]) -> dict[str, Any]:
    wanted = str(params["menu_item_id"])
    for d in facts.menu:
        if str(d["menu_item_id"]) == wanted:
            return d
    raise LookupError(f"menu item {wanted} isn't on this restaurant's menu")


def _description_messages(facts: Facts, params: dict[str, Any], fb: list[str]) -> list[BaseMessage]:
    d = _item(facts, params)
    body = [_restaurant_line(facts), "The dish:", _dish_line("D1", d, facts)]
    return _messages("content_description", body + _owner_note(params, "notes"), fb)


def _description_check(out: DescriptionOut, facts: Facts, params: dict[str, Any]) -> Checked:
    d = _item(facts, params)
    text = " ".join(out.description.split())
    problems = []
    if not 10 <= len(text) <= 400:
        problems.append("the description must be 10–400 characters")
    spice = int(d.get("spice_level") or 0)
    if spice >= 2 and _MILD.search(text):
        problems.append(f"it is spice level {spice}: don't call it mild")
    if spice == 0 and _SPICY.search(text):
        problems.append("it is not spicy: don't call it spicy or hot")
    tags = set(d.get("dietary_tags") or [])
    problems += [
        f"it isn't tagged {t}" for t, rx in _DIETS.items() if rx.search(text) and t not in tags
    ]
    if extra := _extra_numbers(text, _dish_line("D1", d, facts)):
        problems.append(f"numbers not in the facts: {', '.join(sorted(extra))}")
    if problems:
        return None, problems
    return {
        "menu_item_id": d["menu_item_id"],
        "name": d["name"],
        "before": d.get("description"),
        "description": text,
    }, []


# Promotion ----------------------------------------------------------------

_CODE = re.compile(r"^[A-Z0-9]{3,20}$")


def _promotion_messages(facts: Facts, params: dict[str, Any], fb: list[str]) -> list[BaseMessage]:
    menu = "\n".join(_dish_line(c, d, facts) for c, d in codes(facts).items())
    prices = [float(d["price"]) for d in facts.menu] or [0]
    body = [
        _restaurant_line(facts),
        f"Menu prices from {rs(min(prices))} to {rs(max(prices))}.",
        fence("menu_text", menu),
    ]
    return _messages("content_promotion", body + _owner_note(params, "goal"), fb)


def _promotion_check(out: PromotionOut, facts: Facts, params: dict[str, Any]) -> Checked:
    problems = []
    code = re.sub(r"[^A-Z0-9]", "", out.code.upper())
    if not _CODE.match(code):
        problems.append("the code must be 3–20 letters or digits")
    top = max((float(d["price"]) for d in facts.menu), default=0)
    if out.discount_type == "percentage" and not 5 <= out.discount_value <= 50:
        problems.append("a percentage discount must be 5–50")
    if out.discount_type == "fixed_amount" and not 0 < out.discount_value <= top / 2:
        problems.append(f"a fixed discount must be above 0 and at most {rs(top / 2)}")
    if not 0 <= out.starts_in_days <= 30:
        problems.append("it must start within 30 days")
    if not 1 <= out.lasts_days <= 60:
        problems.append("it must last 1–60 days")
    if out.max_uses is not None and not 1 <= out.max_uses <= 10_000:
        problems.append("max uses must be 1–10000, or none")
    value = round(out.discount_value, 2)
    pitch = " ".join(out.pitch.split())
    shown = f"{value:g}% off" if out.discount_type == "percentage" else f"{rs(value)} off"
    allowed = f"{code} {value:g} {shown} {out.lasts_days} {out.max_uses or ''} " + " ".join(
        _dish_line("D", d, facts) for d in facts.menu
    )
    if not pitch or len(pitch) > 200:
        problems.append("the pitch must be one line, under 200 characters")
    elif extra := _extra_numbers(pitch, allowed):
        problems.append(f"numbers in the pitch not in the offer: {', '.join(sorted(extra))}")
    if problems:
        return None, problems
    # Dates in code: from midnight Pakistan time, to the end of the last day.
    today = datetime.now(_PKT).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today + timedelta(days=out.starts_in_days)
    end = start + timedelta(days=out.lasts_days) - timedelta(seconds=1)
    return {
        "code": code,
        "discount_type": out.discount_type,
        "discount_value": value,
        "valid_from": start.astimezone(UTC).isoformat(),
        "valid_until": end.astimezone(UTC).isoformat(),
        "max_uses": out.max_uses,
        "pitch": pitch,
        "summary": f"{shown}, {start:%d %b} – {end:%d %b}",
    }, []


# Dish picks ---------------------------------------------------------------


def _picks_messages(facts: Facts, params: dict[str, Any], fb: list[str]) -> list[BaseMessage]:
    menu = "\n".join(_dish_line(c, d, facts) for c, d in codes(facts).items())
    praise = [f"{r.rating}/5: {r.text[:300]}" for r in facts.reviews[:30]]
    body = [_restaurant_line(facts), fence("menu_text", menu)]
    if praise:
        body.append(fence("reviews", "\n".join(praise)))
    return _messages("content_dishes", body + _owner_note(params, "goal"), fb)


def _picks_check(out: DishPicksOut, facts: Facts, params: dict[str, Any]) -> Checked:
    by_code = codes(facts)
    picks: list[dict[str, Any]] = []
    unknown: list[str] = []
    for p in out.picks[:5]:
        d = by_code.get(p.code.strip().strip("[]").upper())
        if d is None:
            unknown.append(p.code)
        elif all(x["menu_item_id"] != d["menu_item_id"] for x in picks):
            picks.append(
                {
                    "menu_item_id": d["menu_item_id"],
                    "name": d["name"],
                    "price": float(d["price"]),
                    "reason": " ".join(p.reason.split())[:200],
                }
            )
    if unknown:
        return None, [f"{', '.join(unknown)} aren't dish codes from the menu"]
    if not picks:
        return None, ["pick 3–5 dishes from the menu"]
    return {"picks": picks}, []


# Engagement message -------------------------------------------------------


def _message_facts(facts: Facts, params: dict[str, Any]) -> list[str]:
    lines = [_restaurant_line(facts)]
    if facts.menu:
        lines.append(
            fence(
                "menu_text",
                "\n".join(_dish_line(c, d, facts) for c, d in list(codes(facts).items())[:15]),
            )
        )
    lines.append(f"Tone: {params.get('tone') or 'friendly'}.")
    return lines + _owner_note(params, "about")


def _message_messages(facts: Facts, params: dict[str, Any], fb: list[str]) -> list[BaseMessage]:
    return _messages("content_message", _message_facts(facts, params), fb)


def _message_check(out: MessageOut, facts: Facts, params: dict[str, Any]) -> Checked:
    text = " ".join(out.message.split())
    problems = []
    if not 10 <= len(text) <= 300:
        problems.append("the message must be 10–300 characters")
    allowed = " ".join([*_message_facts(facts, params), str(params.get("about") or "")])
    if extra := _extra_numbers(text, allowed):
        problems.append(
            f"numbers not in the facts (no made-up prices or discounts): {', '.join(sorted(extra))}"
        )
    return (None, problems) if problems else ({"message": text}, [])


# Feedback summary: batches of reviews, then combined (map → reduce) -------


def batches(facts: Facts) -> list[list[Review]]:
    size = settings.content_review_batch
    return [facts.reviews[i : i + size] for i in range(0, len(facts.reviews), size)]


def batch_messages(batch: list[Review], facts: Facts) -> list[BaseMessage]:
    text = "\n".join(f"- {r.rating}/5: {r.text}" for r in batch)
    names = ", ".join(d["name"] for d in facts.menu)
    body = [_restaurant_line(facts), f"Dishes on the menu: {names}.", fence("reviews", text)]
    return _messages("content_feedback_batch", body, [])


def _feedback_messages(facts: Facts, params: dict[str, Any], fb: list[str]) -> list[BaseMessage]:
    notes: list[FeedbackBatchOut] = params["_batches"]
    lines = []
    for n, b in enumerate(notes, 1):
        lines.append(
            f"Batch {n}: praise: {'; '.join(b.praise) or '-'} | complaints: "
            f"{'; '.join(b.complaints) or '-'} | "
            f"dishes praised: {', '.join(b.dishes_praised) or '-'}"
            f" | dishes criticised: {', '.join(b.dishes_criticised) or '-'}"
        )
    body = [_restaurant_line(facts), fence("review_notes", "\n".join(lines))]
    return _messages("content_feedback_combine", body, fb)


def _feedback_check(out: FeedbackOut, facts: Facts, params: dict[str, Any]) -> Checked:
    avg, n = facts.rating
    summary = " ".join(out.summary.split())
    if not summary:
        return None, ["write the summary"]
    if extra := _extra_numbers(summary, _restaurant_line(facts)):
        return None, [
            f"numbers not in the facts: {', '.join(sorted(extra))} (counts are added by us)"
        ]

    def clean(xs: list[str]) -> list[str]:
        return [" ".join(x.split())[:200] for x in xs if x.strip()][:5]

    return {
        "reviews_read": n,
        "average_rating": avg,
        "summary": summary,
        "strengths": clean(out.strengths),
        "complaints": clean(out.complaints),
        "actions": clean(out.actions),
    }, []


KINDS: dict[str, Kind] = {
    "menu_description": Kind(
        DescriptionOut, "content_description", "fast", _description_messages, _description_check
    ),
    "promotion": Kind(
        PromotionOut,
        "content_promotion",
        "smart",
        _promotion_messages,
        _promotion_check,
        needs_orders=True,
    ),
    "dish_picks": Kind(
        DishPicksOut,
        "content_dishes",
        "smart",
        _picks_messages,
        _picks_check,
        needs_reviews=True,
        needs_orders=True,
    ),
    "engagement_message": Kind(
        MessageOut, "content_message", "fast", _message_messages, _message_check
    ),
    "feedback_summary": Kind(
        FeedbackOut,
        "content_feedback_combine",
        "smart",
        _feedback_messages,
        _feedback_check,
        needs_reviews=True,
    ),
}
