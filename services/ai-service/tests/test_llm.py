"""The provider-agnostic LLM layer (docs/week5-plan.md B5-D1): models by
role from config, provider errors mapped to our own, the smart → fast
fallback, and usage/cost recording. No network and no key: models are
built but never called, or replaced by LangChain's fake chat models."""

from typing import Any

import anthropic
import httpx2
import openai
import pytest
from app import llm as llm_module
from app import metrics
from app.config import Settings
from app.llm import (
    LLM,
    LLMAuthError,
    LLMBadOutput,
    LLMBadRequest,
    LLMNotConfigured,
    LLMRateLimited,
    LLMUnavailable,
    Usage,
    classify,
    map_errors,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "service_name": "ai-service",
        "database_url": "postgresql+asyncpg://x/y",
        "llm_api_key": SecretStr("test-key"),
    }
    return Settings(_env_file=None, **(base | overrides))  # type: ignore[arg-type,call-arg]


# --- Building models from config ---


def test_anthropic_defaults_by_role() -> None:
    llm = LLM(_settings())
    smart, fast = llm.model("smart"), llm.model("fast")
    assert isinstance(smart, ChatAnthropic) and isinstance(fast, ChatAnthropic)
    assert (smart.model, fast.model) == ("claude-sonnet-5-5", "claude-haiku-5-5")
    assert (smart.reasoning_effort, fast.reasoning_effort) == ("medium", "low")
    assert (smart.max_tokens, fast.max_tokens) == (4096, 2048)
    # Newer Claude models reject non-default sampling settings.
    assert smart.temperature is None
    assert llm.model("smart") is smart  # built once


def test_openai_by_config_with_base_url() -> None:
    llm = LLM(
        _settings(
            llm_provider="openai",
            llm_model_smart="big-model",
            llm_model_fast="small-model",
            llm_base_url="http://vllm:8000/v1",
            llm_effort_smart="omit",
            llm_effort_fast="none",
        )
    )
    fast = llm.model("fast")
    assert isinstance(fast, ChatOpenAI)
    assert fast.model_name == "small-model"
    assert fast.openai_api_base == "http://vllm:8000/v1"
    assert fast.reasoning_effort == "none"  # sent: thinking off
    assert llm.model("smart").reasoning_effort is None  # omitted


def test_openai_needs_explicit_models() -> None:
    with pytest.raises(LLMNotConfigured, match="LLM_MODEL_SMART"):
        LLM(_settings(llm_provider="openai")).model("smart")


def test_no_key_means_not_configured() -> None:
    llm = LLM(_settings(llm_api_key=SecretStr("")))
    assert not llm.configured
    with pytest.raises(LLMNotConfigured):
        llm.model("fast")


def test_a_base_url_alone_is_enough_for_a_local_server() -> None:
    llm = LLM(
        _settings(
            llm_provider="openai",
            llm_api_key=SecretStr(""),
            llm_base_url="http://ollama:11434/v1",
            llm_model_fast="llama",
        )
    )
    assert llm.configured
    assert isinstance(llm.model("fast"), ChatOpenAI)


_PROVIDER_IMPORTS = ("langchain_anthropic", "langchain_openai", "import anthropic", "import openai")


def test_only_llm_module_names_a_provider() -> None:
    """B5-D1: the rest of ai-service depends on BaseChatModel only."""
    import pathlib

    app_dir = pathlib.Path(llm_module.__file__).parent
    offenders = [
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if p.name != "llm.py" and any(name in p.read_text() for name in _PROVIDER_IMPORTS)
    ]
    assert offenders == []


# --- Error mapping ---

_REQ = httpx2.Request("POST", "https://llm.example/v1/messages")


def _status_error(sdk: Any, cls: str, status: int) -> Exception:
    return getattr(sdk, cls)("boom", response=httpx2.Response(status, request=_REQ), body=None)


@pytest.mark.parametrize("sdk", [anthropic, openai])
@pytest.mark.parametrize(
    ("cls", "status", "expected"),
    [
        ("AuthenticationError", 401, LLMAuthError),
        ("PermissionDeniedError", 403, LLMAuthError),
        ("RateLimitError", 429, LLMRateLimited),
        ("BadRequestError", 400, LLMBadRequest),
        ("NotFoundError", 404, LLMBadRequest),
        ("InternalServerError", 500, LLMUnavailable),
        ("InternalServerError", 529, LLMUnavailable),  # Anthropic "overloaded"
    ],
)
def test_status_errors_map_the_same_for_both_providers(
    sdk: Any, cls: str, status: int, expected: type
) -> None:
    assert type(classify(_status_error(sdk, cls, status))) is expected


@pytest.mark.parametrize("sdk", [anthropic, openai])
def test_connection_errors_and_timeouts_are_unavailable(sdk: Any) -> None:
    assert type(classify(sdk.APIConnectionError(request=_REQ))) is LLMUnavailable
    assert type(classify(sdk.APITimeoutError(request=_REQ))) is LLMUnavailable
    assert type(classify(TimeoutError())) is LLMUnavailable


def test_a_bug_is_not_an_llm_error() -> None:
    assert classify(KeyError("x")) is None
    with pytest.raises(KeyError), map_errors():
        raise KeyError("x")


def test_map_errors_keeps_the_cause() -> None:
    original = _status_error(anthropic, "RateLimitError", 429)
    with pytest.raises(LLMRateLimited) as info, map_errors():
        raise original
    assert info.value.__cause__ is original


# --- Structured output and fallback (fake models) ---


class Pick(BaseModel):
    item_id: str


class _Answers(GenericFakeChatModel):
    """Answers structured-output calls with a fixed object."""

    answer: Any = None

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        from langchain_core.runnables import RunnableLambda

        async def respond(_: Any) -> Any:
            # Through the model, so its callbacks (usage recording) run.
            await self.ainvoke("x")
            return self.answer

        return RunnableLambda(respond)


class _Failing(_Answers):
    """Fails every call, structured or not, with the given provider error."""

    error: Any = None

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error

    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error


def _fake_llm(models: dict[str, BaseChatModel], **overrides: Any) -> LLM:
    return LLM(_settings(**overrides), factory=lambda role: models[role])


def _ok(answer: Any) -> _Answers:
    return _Answers(messages=iter([AIMessage("{}")] * 10), answer=answer)


async def test_structured_returns_the_schema_object() -> None:
    llm = _fake_llm({"smart": _ok(Pick(item_id="i-1")), "fast": _ok(None)})
    assert await llm.structured("smart", Pick).ainvoke("q") == Pick(item_id="i-1")


async def test_smart_falls_back_to_fast_on_a_transient_error() -> None:
    smart = _Failing(messages=iter([]), error=_status_error(anthropic, "RateLimitError", 429))
    llm = _fake_llm({"smart": smart, "fast": _ok(Pick(item_id="from-fast"))})
    assert await llm.structured("smart", Pick).ainvoke("q") == Pick(item_id="from-fast")


async def test_no_fallback_for_a_bad_key() -> None:
    smart = _Failing(messages=iter([]), error=_status_error(anthropic, "AuthenticationError", 401))
    llm = _fake_llm({"smart": smart, "fast": _ok(Pick(item_id="never"))})
    with pytest.raises(LLMAuthError):
        await llm.structured("smart", Pick).ainvoke("q")


async def test_fallback_can_be_turned_off() -> None:
    smart = _Failing(messages=iter([]), error=_status_error(openai, "InternalServerError", 503))
    llm = _fake_llm(
        {"smart": smart, "fast": _ok(Pick(item_id="never"))}, llm_fallback_to_fast=False
    )
    with pytest.raises(LLMUnavailable):
        await llm.structured("smart", Pick).ainvoke("q")


async def test_a_missing_structured_answer_is_bad_output() -> None:
    llm = _fake_llm({"smart": _ok(None), "fast": _ok(None)})
    with pytest.raises(LLMBadOutput):
        await llm.structured("fast", Pick).ainvoke("q")


# --- Usage and cost ---


def _value(metric: Any, *labels: str) -> float:
    return metric.labels(*labels)._value.get()


def test_usage_splits_cached_input_out() -> None:
    usage = Usage.from_metadata(
        {
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "input_token_details": {"cache_read": 600, "cache_creation": 100},
        }
    )
    assert usage == Usage(input=300, output=200, cache_read=600, cache_write=100)
    # Sonnet 5.5: 300 x $2 + 200 x $10 + 600 x $0.20 + 100 x $2.50, per million.
    assert usage.cost_usd("claude-sonnet-5-5") == pytest.approx(0.00297)
    assert usage.cost_usd("unknown-model") is None


def test_reasoning_tokens_are_counted_within_output() -> None:
    usage = Usage.from_metadata(
        {
            "input_tokens": 500,
            "output_tokens": 300,
            "total_tokens": 800,
            "output_token_details": {"reasoning": 250},
        }
    )
    assert (usage.output, usage.reasoning) == (300, 250)


def test_turn_usage_adds_up_per_step() -> None:
    from app.llm import TurnUsage

    turn = TurnUsage()
    turn.add("m", Usage(input=100, output=10), step="pick", ms=5)
    turn.add("m", Usage(input=120, output=20, reasoning=15), step="pick", ms=7)
    turn.add("m", Usage(input=50, output=5, cache_read=40), step="explain", ms=3)
    assert turn.steps == {
        "pick": {"calls": 2, "input": 220, "cached": 0, "output": 30, "reasoning": 15, "ms": 12},
        "explain": {"calls": 1, "input": 90, "cached": 40, "output": 5, "reasoning": 0, "ms": 3},
    }
    assert (turn.calls, turn.input_tokens, turn.output_tokens) == (3, 310, 35)


async def test_every_call_records_outcome_tokens_and_cost() -> None:
    reply = AIMessage(
        "Try the chicken karahi.",
        usage_metadata={"input_tokens": 500, "output_tokens": 40, "total_tokens": 540},
        response_metadata={"model_name": "claude-haiku-5-5"},
    )
    llm = _fake_llm({"fast": GenericFakeChatModel(messages=iter([reply]))})
    ok_before = _value(metrics.LLM_CALLS, "anthropic", "fast", "fast", "ok")
    in_before = _value(metrics.LLM_TOKENS, "anthropic", "claude-haiku-5-5", "input")
    cost_before = _value(metrics.LLM_COST_USD, "anthropic", "claude-haiku-5-5")

    await llm.model("fast").ainvoke("something spicy")

    assert _value(metrics.LLM_CALLS, "anthropic", "fast", "fast", "ok") == ok_before + 1
    assert _value(metrics.LLM_TOKENS, "anthropic", "claude-haiku-5-5", "input") == in_before + 500
    # 500 x $0.10 + 40 x $0.50, per million.
    assert _value(metrics.LLM_COST_USD, "anthropic", "claude-haiku-5-5") == pytest.approx(
        cost_before + 0.00007
    )


async def test_failed_calls_are_counted_by_type() -> None:
    smart = _Failing(messages=iter([]), error=_status_error(anthropic, "RateLimitError", 429))
    llm = _fake_llm({"smart": smart})
    before = _value(metrics.LLM_CALLS, "anthropic", "smart", "smart", "rate_limited")
    with pytest.raises(LLMRateLimited), map_errors():
        await llm.model("smart").ainvoke("q")
    assert _value(metrics.LLM_CALLS, "anthropic", "smart", "smart", "rate_limited") == before + 1
