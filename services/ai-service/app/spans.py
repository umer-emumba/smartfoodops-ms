"""Trace spans for the AI work (docs/week5-plan.md step 7): one per
assistant graph step, one per LLM call (app/llm.py), and one each for an
order explanation's wording and a content draft's generation.

They join the request's trace (FastAPI and Celery are instrumented by
sfo_observability), so Jaeger shows which step of a turn was slow and which
model it called. Attributes are counts, codes and model names only — never
a message, review or answer (B5-D8, B5-D37).

When tracing is off (tests, local runs) the tracer is OpenTelemetry's no-op
one: the spans cost nothing."""

import functools
from collections.abc import Callable, Mapping
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("ai-service")

# State keys whose size says what a step did (never their content).
_COUNTED = (
    "candidates",
    "restaurant_candidates",
    "dishes",
    "restaurants",
    "feedback",
)
_CODES = ("status", "degraded_reason")
_NUMBERS = ("attempts", "made_up")


def step_attributes(update: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a graph step returned, as span attributes: its intent, status
    and reason codes, and how many candidates / cards / problems — no text."""
    if not update:
        return {}
    out: dict[str, Any] = {}
    understanding = update.get("understanding")
    if understanding is not None and getattr(understanding, "intent", None):
        out["ai.intent"] = str(understanding.intent)
    for key in _CODES:
        if isinstance(update.get(key), str):
            out[f"ai.{key}"] = update[key]
    for key in _NUMBERS:
        if isinstance(update.get(key), int):
            out[f"ai.{key}"] = update[key]
    for key in _COUNTED:
        if isinstance(update.get(key), list | dict):
            out[f"ai.{key}.count"] = len(update[key])
    if "pick" in update:
        out["ai.picked"] = update["pick"] is not None
    return out


def traced_step(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wraps a graph node in a span `assistant.<name>`. `functools.wraps`
    keeps the node's signature, so LangGraph still passes `runtime` to the
    nodes that take it."""

    @functools.wraps(fn)
    async def node(*args: Any, **kwargs: Any) -> Any:
        with tracer.start_as_current_span(f"assistant.{name}") as span:
            update = await fn(*args, **kwargs)
            span.set_attributes(step_attributes(update))
            return update

    return node


def fail(span: trace.Span, code: str) -> None:
    """Marks a span as failed with a short code (not the exception text,
    which may quote the model's output)."""
    span.set_attribute("error.type", code)
    span.set_status(Status(StatusCode.ERROR, code))
