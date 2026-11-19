"""Putting an order's facts into words (docs/week5-plan.md step 4): the
fast model rewords the diagnosis (app/assistant/order_facts.py) and the
number guard checks it.

The number guard: every number in the model's text (amounts, minutes,
times, the order number) must appear in the facts. If one doesn't — an
invented ETA, a sum the model worked out itself — the code-written
explanation is used instead. So the text is generated in full and then
streamed: a wrong number can't be taken back once shown. It is 2–3
sentences, about a second."""

import logging
import re
from decimal import Decimal
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app import metrics
from app.assistant.order_facts import Diagnosis
from app.llm import LLM, LLMError, map_errors
from app.prompts import fence, load

logger = logging.getLogger(__name__)

PROMPTS = ("assistant_rules", "order_explain")

_NUMBER = re.compile(r"\d{1,2}:\d{2}|\d[\d,]*(?:\.\d+)?")

# Fallback reasons that still show a correct (code-written) answer.
CORRECT_FALLBACKS = (None, "number_guard", "empty")


def numbers(text: str) -> set[str]:
    """Numbers as written, normalised: "Rs 1,700" → "1700", "450.00" →
    "450"; times kept as "19:40"."""
    out = set()
    for raw in _NUMBER.findall(text):
        if ":" in raw:
            out.add(raw)
            continue
        value = Decimal(raw.replace(",", "").rstrip("."))
        out.add(format(value.normalize(), "f"))
    return out


def invented_numbers(text: str, d: Diagnosis) -> set[str]:
    allowed = numbers(" ".join([*d.facts, d.text, f"#{d.order_number}", *d.notes]))
    return numbers(text) - allowed


def _messages(d: Diagnosis) -> list[BaseMessage]:
    lines = [f"Order #{d.order_number}, status: {d.status.replace('_', ' ')}.", "Facts:"]
    lines += [f"- {f}" for f in d.facts]
    lines += [fence("note", n) for n in d.notes]
    rules, task = load("assistant_rules").text, load("order_explain").text
    return [SystemMessage(f"{rules}\n\n{task}"), HumanMessage("\n".join(lines))]


async def word(d: Diagnosis, llm: LLM) -> tuple[str, str | None]:
    """The explanation to show, and why the code's own text was used
    (None when the model's wording passed)."""
    if not llm.configured:
        metrics.ORDER_EXPLANATIONS.labels("no_llm").inc()
        return d.text, "llm_not_configured"
    try:
        with map_errors():
            reply = await llm.model("fast").ainvoke(_messages(d))
    except LLMError as exc:
        logger.warning("order explanation wording failed: %s", exc.code)
        metrics.ORDER_EXPLANATIONS.labels("llm_failed").inc()
        return d.text, exc.code
    text = " ".join(str(reply.text).split())
    if not text:
        metrics.ORDER_EXPLANATIONS.labels("llm_failed").inc()
        return d.text, "empty"
    if extra := invented_numbers(text, d):
        logger.info("order explanation had numbers not in the facts %s; using code's", extra)
        metrics.ORDER_EXPLANATIONS.labels("number_guard").inc()
        return d.text, "number_guard"
    metrics.ORDER_EXPLANATIONS.labels("worded").inc()
    return text, None


def chunks(text: str, size: int = 6) -> list[str]:
    """Words in small groups, so the checked text still arrives as a stream."""
    words = text.split(" ")
    return [
        " ".join(words[i : i + size]) + (" " if i + size < len(words) else "")
        for i in range(0, len(words), size)
    ]


def facts_event(d: Diagnosis) -> dict[str, Any]:
    return {
        "type": "facts",
        "order_number": d.order_number,
        "status": d.status,
        "situation": d.code,
        "facts": d.facts,
    }
