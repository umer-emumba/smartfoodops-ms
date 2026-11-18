"""Fakes for the assistant's tests: scripted chat models (structured answers
by schema, streamed text), a retriever over a fixed menu, and live
restaurant-service reads — no key, no Qdrant, no network."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.assistant.graph import Deps
from app.config import Settings
from app.llm import LLM
from app.restaurant_client import LiveItem, LiveRestaurant
from app.retrieval import Filters, Hit, Load
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import SecretStr


class ScriptedChat(GenericFakeChatModel):
    """Structured calls answer from `answers[schema name]` in order (an
    exception in the list is raised instead); plain calls stream
    `messages`. Every structured call's input is kept in `asked`."""

    answers: dict[str, list[Any]] = {}
    asked: list[tuple[str, Any]] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        async def respond(value: Any) -> Any:
            self.asked.append((schema.__name__, value))
            queue = self.answers.get(schema.__name__) or []
            if not queue:
                raise AssertionError(f"No scripted answer for {schema.__name__}")
            answer = queue.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return RunnableLambda(respond)


class BrokenStream(ScriptedChat):
    """Streams one chunk of text, then fails with `error`."""

    error: Any = None

    async def _astream(self, *args: Any, **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        yield ChatGenerationChunk(message=AIMessageChunk(content="Try the"))
        raise self.error


class StreamsUsage(ScriptedChat):
    """Streams `text` as one chunk carrying usage, as provider streams do."""

    text: str = ""
    usage: dict[str, int] = {}
    model_name: str = ""

    async def _astream(self, *args: Any, **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=self.text,
                usage_metadata=self.usage,  # type: ignore[arg-type]
                response_metadata={"model_name": self.model_name},
            )
        )


def chat(*structured: Any, text: str = "These fit what you asked for.") -> ScriptedChat:
    """Structured answers by type: `Understanding(...)`, `Pick(...)`, or
    `("Pick", exception)` for a failing call."""
    answers: dict[str, list[Any]] = {}
    for a in structured:
        key, value = a if isinstance(a, tuple) else (type(a).__name__, a)
        answers.setdefault(key, []).append(value)
    return ScriptedChat(messages=iter([AIMessage(text)] * 5), answers=answers, asked=[])


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "service_name": "ai-service",
        "database_url": "postgresql+asyncpg://x/y",
        "llm_api_key": SecretStr("test-key"),
    }
    return Settings(_env_file=None, **(base | over))  # type: ignore[arg-type,call-arg]


def llm(smart: Any, fast: Any, **over: Any) -> LLM:
    models = {"smart": smart, "fast": fast}
    return LLM(settings(**over), factory=lambda role: models[role])


def dish(item_id: str, name: str, price: float, **over: Any) -> Hit:
    data = {
        "menu_item_id": item_id,
        "restaurant_id": "r-1",
        "restaurant_name": "Desi Dastarkhwan",
        "restaurant_status": "active",
        "cuisine": "Pakistani",
        "name": name,
        "description": f"{name}, freshly made.",
        "price": price,
        "dietary_tags": ["halal"],
        "spice_level": 1,
        "is_available": True,
        "in_stock": True,
        **over,
    }
    return Hit(kind="item", id=item_id, score=1.0, text=name, data=data)


MENU = [
    dish("m-1", "Chicken Qorma", 900),
    dish("m-2", "Daal Chawal", 450, dietary_tags=["vegetarian", "halal"], spice_level=0),
    dish("m-3", "Malai Boti", 1050, spice_level=0),
    dish("m-4", "Pad Thai", 1650, restaurant_id="r-2", restaurant_name="Wok & Basil"),
    dish("m-5", "Naan", 80, dietary_tags=["vegetarian"], spice_level=0),
]


class FakeRetriever:
    def __init__(
        self,
        dishes: Sequence[Hit] = tuple(MENU),
        restaurants: Sequence[Hit] = (),
        profile: str | None = "Customer order history: likes mild curries.",
        history: Sequence[Hit] = (),
        fail: BaseException | None = None,
    ) -> None:
        self.dishes, self.restaurants, self.profile = list(dishes), list(restaurants), profile
        self.history, self.fail = list(history), fail
        self.searches: list[tuple[str, Filters]] = []

    async def retrieve(self, query: str, filters: Filters) -> list[Hit]:
        if self.fail:
            raise self.fail
        self.searches.append((query, filters))
        if filters.customer_id is not None:
            return self.history
        return self.dishes[: filters.limit]

    async def suggest_restaurants(self, query: str, filters: Filters) -> list[Hit]:
        self.searches.append((query, filters))
        return self.restaurants

    async def customer_profile(self, customer_id: str) -> str | None:
        return self.profile

    async def items_by_id(self, ids: Sequence[str]) -> list[Hit]:
        return [h for h in self.dishes if h.id in ids]


class Live:
    """restaurant-service's live reads; `prices` overrides index prices,
    `closed` / `gone` / `loads` change availability."""

    def __init__(
        self,
        prices: dict[str, float] | None = None,
        gone: set[str] | None = None,
        closed: set[str] | None = None,
        loads: dict[str, Load] | None = None,
        down: bool = False,
    ) -> None:
        self.prices, self.gone, self.closed = prices or {}, gone or set(), closed or set()
        self.load_table, self.down = loads or {}, down
        self.quoted: list[dict[str, list[str]]] = []

    async def quotes(self, by_restaurant: dict[str, list[str]]) -> dict[str, LiveRestaurant]:
        self.quoted.append(by_restaurant)
        if self.down:
            return {}
        index = {h.id: h.data for h in MENU}
        return {
            rid: LiveRestaurant(
                open=rid not in self.closed,
                items={
                    i: LiveItem(
                        price=Decimal(str(self.prices.get(i, index.get(i, {}).get("price", 0)))),
                        orderable=i not in self.gone,
                    )
                    for i in ids
                },
            )
            for rid, ids in by_restaurant.items()
        }

    async def loads(self, ids: Sequence[str]) -> dict[str, Load]:
        return {i: self.load_table[i] for i in ids if i in self.load_table}


def deps(smart: Any, fast: Any, retriever: Any = None, live: Live | None = None, **over: Any):
    live = live or Live()
    return Deps(
        llm=llm(smart, fast, **over),
        retriever=retriever or FakeRetriever(),  # type: ignore[arg-type]
        quotes=live.quotes,
        loads=live.loads,
    )
