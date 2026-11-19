"""Trace spans (docs/week5-plan.md step 7): one per graph step with what it
did, one per LLM call with model and tokens — and never any text (B5-D37)."""

from collections.abc import Iterator

import pytest
from app.assistant.schemas import Constraints, Pick, PickedItem, Understanding
from app.spans import step_attributes
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.assistant_fakes import chat, deps
from tests.test_assistant_graph import _run

_EXPORTER = InMemorySpanExporter()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    # The global provider can be set once per process; app.spans' tracer is
    # a proxy that starts using it as soon as it is.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def _texts(span: ReadableSpan) -> list[str]:
    return [v for v in (span.attributes or {}).values() if isinstance(v, str)]


async def test_a_turn_has_a_span_per_step_and_per_llm_call(spans) -> None:
    understood = Understanding(
        intent="recommend_dishes", search_query="karahi", constraints=Constraints()
    )
    pick = Pick(items=[PickedItem(item_id="m-1", quantity=1, reason="fits")])
    secret = "my secret craving for karahi"
    fast = chat(understood, text="Spicy and SECRET-ANSWER.")
    await _run(deps(chat(pick), fast), secret)

    done = spans.get_finished_spans()
    names = [s.name for s in done]
    for step in ("understand", "retrieve", "pick", "check", "explain"):
        assert f"assistant.{step}" in names
    by_name = {s.name: s for s in done}
    assert by_name["assistant.understand"].attributes["ai.intent"] == "recommend_dishes"
    assert by_name["assistant.retrieve"].attributes["ai.candidates.count"] > 0
    assert by_name["assistant.check"].attributes["ai.dishes.count"] == 1

    # The streamed explanation is a real chat-model call: an LLM span under
    # the explain step, named after OpenTelemetry's GenAI conventions.
    llm = by_name["llm fast"]
    assert llm.attributes["ai.step"] == "explain"
    assert llm.attributes["gen_ai.operation.name"] == "chat"
    assert llm.parent is not None
    assert llm.parent.span_id == by_name["assistant.explain"].context.span_id

    # No message or answer text anywhere.
    for span in done:
        assert not any("secret" in t.lower() for t in _texts(span)), span.name


async def test_a_failed_llm_call_marks_its_span(spans) -> None:
    from tests.assistant_fakes import BrokenStream
    from tests.test_assistant_graph import _api_error

    understood = Understanding(
        intent="recommend_dishes", search_query="karahi", constraints=Constraints()
    )
    pick = Pick(items=[PickedItem(item_id="m-1", quantity=1, reason="fits")])
    fast = BrokenStream(
        messages=iter([]),
        answers={"Understanding": [understood]},
        asked=[],
        error=_api_error("RateLimitError", 429),
    )
    await _run(deps(chat(pick), fast), "karahi")

    [llm] = [s for s in spans.get_finished_spans() if s.name == "llm fast"]
    assert llm.attributes["error.type"] == "rate_limited"
    assert llm.status.status_code == trace.StatusCode.ERROR


def test_step_attributes_are_counts_and_codes_only() -> None:
    update = {
        "text": "a whole answer",
        "message": "what the customer typed",
        "dishes": [1, 2],
        "status": "degraded",
        "degraded_reason": "llm_rate_limited",
        "attempts": 2,
        "pick": None,
    }
    assert step_attributes(update) == {
        "ai.status": "degraded",
        "ai.degraded_reason": "llm_rate_limited",
        "ai.attempts": 2,
        "ai.dishes.count": 2,
        "ai.picked": False,
    }
