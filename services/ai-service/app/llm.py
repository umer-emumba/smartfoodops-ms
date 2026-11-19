"""The LLM layer (docs/week5-plan.md B5-D1): the only module that names a
provider. Everything else asks for a model by *role* and gets LangChain's
`BaseChatModel`, so switching from Anthropic to OpenAI is configuration
(`LLM_PROVIDER`, `LLM_API_KEY`, models) plus an eval run, not a rewrite.

- `model(role)`: the chat model for a role — used for streamed text.
- `structured(role, schema)`: a runnable that returns a validated `schema`
  instance, with provider errors mapped and a smart → fast fallback.
- `map_errors()`: wraps any other LLM call so callers only ever see the
  `LLMError` types below — retries, fallback and degraded answers are written
  against these, never against an SDK's exception classes.

Every call's latency, tokens and estimated cost are recorded by a callback
attached to each model (`_UsageRecorder`), whichever step made the call.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar
from uuid import UUID

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from opentelemetry.trace import Span, SpanKind
from pydantic import BaseModel, ValidationError
from sfo_common.errors import ServiceUnavailableError

from app import metrics, spans
from app.config import Settings, settings

logger = logging.getLogger(__name__)

Role = Literal["smart", "fast"]
SchemaT = TypeVar("SchemaT", bound=BaseModel)


# --- Our own error types: the only ones the rest of ai-service handles. ---


class LLMError(ServiceUnavailableError):
    """An LLM call failed. A 503 if it ever reaches a client — the caller
    did nothing wrong — but the assistant normally degrades instead."""

    code = "llm_unavailable"
    result = "unavailable"  # the `result` label on ai_llm_calls_total


class LLMNotConfigured(LLMError):
    code = "llm_not_configured"
    result = "not_configured"


class LLMRateLimited(LLMError):
    code = "llm_rate_limited"
    result = "rate_limited"


class LLMUnavailable(LLMError):
    """Provider outage, overload, timeout or network error."""


class LLMAuthError(LLMError):
    """Key missing, wrong, revoked or without access to the model."""

    code = "llm_auth_error"
    result = "auth"


class LLMBadRequest(LLMError):
    """The provider refused the request itself (400/404/413/422): our bug
    or a misconfigured model name — retrying won't help."""

    code = "llm_bad_request"
    result = "bad_request"


class LLMBadOutput(LLMError):
    """The model answered, but not in the shape we asked for."""

    code = "llm_bad_output"
    result = "bad_output"


# Worth retrying on the other model: the fast model has its own rate limit
# and capacity, but a bad key or bad request fails the same way on both.
TRANSIENT: tuple[type[LLMError], ...] = (LLMRateLimited, LLMUnavailable)


def classify(exc: BaseException) -> LLMError | None:
    """The `LLMError` for a provider/LangChain exception, or None when it
    isn't an LLM failure at all (a bug — the caller re-raises it).

    By HTTP status rather than SDK class, so Anthropic's and OpenAI's errors
    (and any OpenAI-compatible server's) map the same way."""
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, OutputParserException | ValidationError):
        return LLMBadOutput("The model's answer didn't match the expected format")
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return LLMAuthError("The LLM provider rejected the API key")
        if status == 429:
            return LLMRateLimited("The LLM provider is rate limiting us")
        if status in (400, 404, 409, 413, 422):
            return LLMBadRequest(f"The LLM provider refused the request ({status})")
        return LLMUnavailable(f"The LLM provider failed ({status})")
    if isinstance(exc, TimeoutError | httpx.TransportError) or _is_sdk_connection_error(exc):
        return LLMUnavailable("The LLM provider can't be reached")
    return None


def _is_sdk_connection_error(exc: BaseException) -> bool:
    """Both SDKs raise APIConnectionError (APITimeoutError is a subclass)
    without a status code. Imported here: a provider SDK that isn't
    installed simply can't have raised it."""
    try:
        import anthropic

        if isinstance(exc, anthropic.APIConnectionError):
            return True
    except ImportError:
        pass
    try:
        import openai

        if isinstance(exc, openai.APIConnectionError):
            return True
    except ImportError:
        pass
    return False


@contextmanager
def map_errors() -> Iterator[None]:
    """Re-raises LLM failures inside the block as `LLMError`s. Works around
    async code too (`with map_errors(): await ...`)."""
    try:
        yield
    except Exception as exc:
        mapped = classify(exc)
        if mapped is None or mapped is exc:
            raise
        raise mapped from exc


# --- Provider defaults and prices. ---


@dataclass(frozen=True)
class _Defaults:
    model: str
    effort: str  # "" = not sent


# Anthropic: model IDs from the Claude API reference (checked 2026-11-17).
# Efforts are starting points, tuned on the assistant eval in step 2. OpenAI
# has no defaults on purpose: its model names weren't checked against its
# docs, so they must be set explicitly (LLM_MODEL_SMART / LLM_MODEL_FAST).
_DEFAULTS: dict[str, dict[Role, _Defaults]] = {
    "anthropic": {
        "smart": _Defaults("claude-sonnet-5-5", "medium"),
        "fast": _Defaults("claude-haiku-5-5", "low"),
    },
    "openai": {},
}


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


# First-party API list prices (Claude API reference, cached 2026-10-06).
# Cache writes are 1.25x input (5-minute TTL), reads as published (Haiku's
# read price is the usual 0.1x of input). Haiku's prices are for prompts up
# to 100K tokens; ours are far smaller. A model missing here costs 0 in the
# metric and logs a warning once — add it rather than guess.
PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
}


@dataclass(frozen=True)
class Usage:
    """One call's tokens, split the way providers bill them."""

    input: int = 0  # uncached input
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0  # the part of `output` spent thinking (billed as output)

    @classmethod
    def from_metadata(cls, meta: dict[str, Any] | None) -> "Usage":
        """From LangChain's `usage_metadata`, whose `input_tokens` already
        includes the cached part (for both providers)."""
        if not meta:
            return cls()
        details = meta.get("input_token_details") or {}
        cache_read = int(details.get("cache_read") or 0)
        cache_write = int(details.get("cache_creation") or 0)
        total_in = int(meta.get("input_tokens") or 0)
        out_details = meta.get("output_token_details") or {}
        return cls(
            input=max(total_in - cache_read - cache_write, 0),
            output=int(meta.get("output_tokens") or 0),
            cache_read=cache_read,
            cache_write=cache_write,
            reasoning=int(out_details.get("reasoning") or 0),
        )

    def cost_usd(self, model: str) -> float | None:
        price = PRICES.get(model)
        if price is None:
            return None
        return (
            self.input * price.input
            + self.output * price.output
            + self.cache_read * price.cache_read
            + self.cache_write * price.cache_write
        ) / 1_000_000


@dataclass
class TurnUsage:
    """Everything the LLM calls of one assistant turn added up — for its
    `ai_interactions` row. Collected by `usage_scope()`."""

    calls: int = 0
    input_tokens: int = 0  # all input, cached or not
    output_tokens: int = 0
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)
    # The same, per graph step (understand, pick, explain, ...): where the
    # tokens of a turn went. A retried step shows calls 2.
    steps: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, model: str, usage: Usage, step: str = "", ms: int = 0) -> None:
        total_in = usage.input + usage.cache_read + usage.cache_write
        self.calls += 1
        self.input_tokens += total_in
        self.output_tokens += usage.output
        self.cost_usd += usage.cost_usd(model) or 0.0
        if model not in self.models:
            self.models.append(model)
        s = self.steps.setdefault(
            step or "other",
            {"calls": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "ms": 0},
        )
        s["calls"] += 1
        s["input"] += total_in
        s["cached"] += usage.cache_read
        s["output"] += usage.output
        s["reasoning"] += usage.reasoning
        s["ms"] += ms


_turn_usage: ContextVar[TurnUsage | None] = ContextVar("turn_usage", default=None)


@contextmanager
def usage_scope() -> Iterator[TurnUsage]:
    """Adds up the usage of every call made inside the block (and in tasks
    it starts — they inherit the context) into the yielded object."""
    totals = TurnUsage()
    token = _turn_usage.set(totals)
    try:
        yield totals
    finally:
        _turn_usage.reset(token)


_unpriced_warned: set[str] = set()


class _UsageRecorder(BaseCallbackHandler):
    """Records every call made through one model: outcome, latency, tokens
    and cost. A callback, so it sees streamed, structured and fallback calls
    alike without each step remembering to."""

    # Run in the caller's thread and context, not an executor: the per-turn
    # totals (`usage_scope`) are found through a context variable.
    run_inline = True

    def __init__(self, provider: str, role: Role, model: str) -> None:
        self.provider, self.role, self.model = provider, role, model
        self._started: dict[UUID, float] = {}
        self._steps: dict[UUID, str] = {}
        self._spans: dict[UUID, Span] = {}

    def on_chat_model_start(
        self,
        serialized: Any,
        messages: Any,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self._started[run_id] = time.perf_counter()
        # LangGraph tags every call made inside a node with the node's name.
        step = str((metadata or {}).get("langgraph_node") or self.role)
        self._steps[run_id] = step
        # A span per call (step 7), named and tagged after OpenTelemetry's
        # GenAI conventions; a child of the graph step's span. Not made
        # current: the callback can't scope it, the HTTP call shows beside it.
        self._spans[run_id] = spans.tracer.start_span(
            f"llm {self.role}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.system": self.provider,
                "gen_ai.request.model": self.model,
                "ai.step": step,
            },
        )

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **_: Any) -> None:
        started = self._started.get(run_id)
        ms = int((time.perf_counter() - started) * 1000) if started is not None else 0
        step = self._steps.pop(run_id, self.role)
        self._finish(run_id, "ok")
        usage = Usage()
        model = self.model
        for generations in response.generations:
            for gen in generations:
                if isinstance(gen, ChatGeneration):
                    meta = getattr(gen.message, "usage_metadata", None)
                    usage = Usage.from_metadata(meta)  # type: ignore[arg-type]
                    model = gen.message.response_metadata.get("model_name") or model
        if (span := self._spans.pop(run_id, None)) is not None:
            span.set_attributes(
                {
                    "gen_ai.response.model": model,
                    "gen_ai.usage.input_tokens": usage.input + usage.cache_read + usage.cache_write,
                    "gen_ai.usage.output_tokens": usage.output,
                }
            )
            span.end()
        self.record_usage(model, usage, step=step, ms=ms)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **_: Any) -> None:
        self._steps.pop(run_id, None)
        mapped = classify(error)
        result = mapped.result if mapped else "error"
        if (span := self._spans.pop(run_id, None)) is not None:
            spans.fail(span, result)
            span.end()
        self._finish(run_id, result)

    def _finish(self, run_id: UUID, result: str) -> None:
        started = self._started.pop(run_id, None)
        metrics.LLM_CALLS.labels(self.provider, self.role, self.model, result).inc()
        if started is not None:
            metrics.LLM_SECONDS.labels(self.provider, self.role, self.model).observe(
                time.perf_counter() - started
            )

    def record_usage(self, model: str, usage: Usage, step: str = "", ms: int = 0) -> None:
        if (turn := _turn_usage.get()) is not None:
            turn.add(model, usage, step=step, ms=ms)
        for kind in ("input", "output", "cache_read", "cache_write"):
            count = getattr(usage, kind)
            if count:
                metrics.LLM_TOKENS.labels(self.provider, model, kind).inc(count)
        cost = usage.cost_usd(model)
        if cost is None:
            if model not in _unpriced_warned:
                _unpriced_warned.add(model)
                logger.warning("No price for LLM model %r; its cost is counted as 0", model)
            return
        metrics.LLM_COST_USD.labels(self.provider, model).inc(cost)


# --- The models. ---

ModelFactory = Callable[[Role], BaseChatModel]


class LLM:
    """Builds and caches one chat model per role. `factory` replaces the
    provider entirely (tests pass LangChain's fake chat models)."""

    def __init__(self, config: Settings, factory: ModelFactory | None = None) -> None:
        self._config = config
        self._factory = factory
        self._models: dict[Role, BaseChatModel] = {}

    @property
    def provider(self) -> str:
        return self._config.llm_provider

    @property
    def configured(self) -> bool:
        """Whether a call can be attempted at all. A local OpenAI-compatible
        server (base URL set) may not need a key."""
        c = self._config
        return bool(self._factory or c.llm_api_key.get_secret_value() or c.llm_base_url)

    def model_name(self, role: Role) -> str:
        c = self._config
        configured = c.llm_model_smart if role == "smart" else c.llm_model_fast
        if configured:
            return configured
        default = _DEFAULTS.get(self.provider, {}).get(role)
        if default is None:
            raise LLMNotConfigured(
                f"No default {role} model for provider {self.provider!r}; "
                f"set LLM_MODEL_{role.upper()}"
            )
        return default.model

    def _effort(self, role: Role) -> str:
        c = self._config
        configured = c.llm_effort_smart if role == "smart" else c.llm_effort_fast
        if configured == "omit":
            return ""
        if configured:
            return configured
        default = _DEFAULTS.get(self.provider, {}).get(role)
        return default.effort if default else ""

    def model(self, role: Role) -> BaseChatModel:
        if role not in self._models:
            if not self.configured:
                raise LLMNotConfigured("No LLM API key is configured (LLM_API_KEY)")
            model = self._factory(role) if self._factory else self._build(role)
            name = self.model_name(role) if not self._factory else role
            recorder = _UsageRecorder(self.provider, role, name)
            model.callbacks = [*(model.callbacks or []), recorder]  # type: ignore[list-item, misc]
            self._models[role] = model
        return self._models[role]

    def _build(self, role: Role) -> BaseChatModel:
        from langchain.chat_models import init_chat_model

        c = self._config
        kwargs: dict[str, Any] = {
            "model_provider": self.provider,
            "api_key": c.llm_api_key.get_secret_value() or "not-needed",
            "max_tokens": c.llm_max_tokens_smart if role == "smart" else c.llm_max_tokens_fast,
            "timeout": c.llm_timeout_seconds,
            "max_retries": c.llm_max_retries,
            # No temperature: newer Claude models reject non-default
            # sampling settings; the defaults are what we want anyway.
        }
        if c.llm_base_url:
            kwargs["base_url"] = c.llm_base_url
        if effort := self._effort(role):
            kwargs["reasoning_effort"] = effort
        return init_chat_model(self.model_name(role), **kwargs)  # type: ignore[return-value]

    def structured(self, role: Role, schema: type[SchemaT]) -> Runnable[Any, SchemaT]:
        """A runnable returning a validated `schema` instance.

        `json_schema` mode — the provider's native structured output, not a
        forced tool call (which newer Claude models reject). Errors come out
        as `LLMError`s; a smart-model call that fails transiently is retried
        once on the fast model (B5-D1)."""

        def build(r: Role) -> Runnable[Any, SchemaT]:
            inner = self.model(r).with_structured_output(schema, method="json_schema")

            async def call(value: Any, config: RunnableConfig) -> SchemaT:
                with map_errors():
                    result = await inner.ainvoke(value, config)
                if not isinstance(result, schema):
                    raise LLMBadOutput("The model returned no structured answer")
                return result

            return RunnableLambda(call, name=f"llm_{r}_{schema.__name__}")

        primary = build(role)
        if role == "smart" and self._config.llm_fallback_to_fast:
            return primary.with_fallbacks([build("fast")], exceptions_to_handle=TRANSIENT)
        return primary


llm = LLM(settings)


async def check_reachable(timeout_seconds: float = 10.0) -> bool:
    """A tiny fast-model call, for drills and the smoke test — not for
    readiness: the assistant degrades without an LLM, it doesn't go down."""
    try:
        with map_errors():
            await asyncio.wait_for(llm.model("fast").ainvoke("Reply with OK."), timeout_seconds)
        return True
    except LLMError:
        return False
