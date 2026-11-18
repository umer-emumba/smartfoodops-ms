"""The real dependencies of a turn: the configured LLM, the shared
retriever (Qdrant + embeddings) and restaurant-service's live reads."""

from app import restaurant_client
from app.assistant.graph import Deps
from app.llm import llm
from app.retrieval import default_retriever

_deps: Deps | None = None


def default_deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = Deps(
            llm=llm,
            retriever=default_retriever(),
            quotes=restaurant_client.quotes,
            loads=restaurant_client.loads,
        )
    return _deps
