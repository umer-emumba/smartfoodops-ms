"""`POST /v1/ai/chat` (step 3) against a real ai_db, with fake models and
retrieval: the SSE stream, errors before it starts, one turn at a time per
conversation, heartbeats, a client going away, and a failure mid-answer."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.api import chat as chat_api
from app.assistant.schemas import Pick, PickedItem, Understanding
from app.assistant.service import ChatRequest, start_turn
from app.config import settings
from app.main import app
from app.models import Conversation, ConversationTurn, Interaction
from httpx import ASGITransport, AsyncClient
from sfo_auth import Role
from sfo_auth.testing import make_test_token
from sqlalchemy import select

from tests.assistant_fakes import FakeRetriever, Live, chat, deps

pytestmark = pytest.mark.db

CUSTOMER = "u-chat"


def _as(user_id: str = CUSTOMER, role: Role = Role.CUSTOMER) -> dict[str, str]:
    token = make_test_token(user_id=user_id, role=role, jwks_url=settings.jwt_jwks_url)
    return {"Authorization": f"Bearer {token}"}


def _models() -> tuple[Any, Any]:
    understood = Understanding(intent="recommend_dishes", search_query="curry")
    picked = Pick(items=[PickedItem(item_id="D1", quantity=1, reason="mild")])
    return chat(picked), chat(understood, text="Try the qorma.")


@pytest.fixture
def use(sessions):
    """Points the route at the test database and the given fake deps."""

    def install(d: Any) -> None:
        app.dependency_overrides[chat_api.get_deps] = lambda: d
        app.dependency_overrides[chat_api.get_sessions] = lambda: sessions

    install(deps(*_models()))
    yield install
    app.dependency_overrides.clear()


async def _post(body: dict[str, Any], headers: dict[str, str] | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/v1/ai/chat", json=body, headers=_as() if headers is None else headers)


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


async def _row(sessions, model: Any, id_: str) -> Any:
    async with sessions() as s:
        return await s.get(model, id_)


# --- The stream ---


async def test_the_answer_streams_in_order(use, sessions) -> None:
    resp = await _post({"message": "mild curry"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _events(resp.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "status" and kinds[-1] == "done"
    assert kinds.index("cards") < kinds.index("token") < kinds.index("done")
    cards = next(d for k, d in events if k == "cards")
    assert [c["item_id"] for c in cards["dishes"]] == ["m-1"]
    done = events[-1][1]
    assert done["status"] == "answered" and done["first_output_ms"] is not None
    conversation = await _row(sessions, Conversation, done["conversation_id"])
    assert conversation.user_id == CUSTOMER and conversation.busy_until is None
    assert (await _row(sessions, Interaction, done["interaction_id"])).status == "answered"


async def test_a_follow_up_continues_the_conversation(use, sessions) -> None:
    first = _events((await _post({"message": "mild curry"})).text)[-1][1]
    use(deps(*_models()))
    resp = await _post(
        {"message": "something cheaper", "conversation_id": first["conversation_id"]}
    )
    assert resp.status_code == 200
    assert _events(resp.text)[-1][1]["conversation_id"] == first["conversation_id"]
    async with sessions() as s:
        turns = (await s.scalars(select(ConversationTurn.seq))).all()
    assert sorted(turns) == [1, 2, 3, 4]


# --- Errors before the stream: plain HTTP ---


async def test_a_busy_conversation_is_409_until_its_mark_expires(use, sessions) -> None:
    first = _events((await _post({"message": "curry"})).text)[-1][1]
    cid = first["conversation_id"]
    async with sessions() as s:
        c = await s.get(Conversation, cid)
        c.busy_until = datetime.now(UTC) + timedelta(seconds=60)
        await s.commit()
    resp = await _post({"message": "and a drink?", "conversation_id": cid})
    assert resp.status_code == 409
    async with sessions() as s:
        c = await s.get(Conversation, cid)
        c.busy_until = datetime.now(UTC) - timedelta(seconds=1)  # the process died
        await s.commit()
    use(deps(*_models()))
    assert (await _post({"message": "and a drink?", "conversation_id": cid})).status_code == 200


async def test_two_messages_at_once_one_gets_through(use, sessions) -> None:
    turn = await start_turn(ChatRequest(user_id=CUSTOMER, message="curry"), sessions)
    from sfo_common.errors import ConflictError

    with pytest.raises(ConflictError):
        await start_turn(
            ChatRequest(user_id=CUSTOMER, message="again", conversation_id=turn.conversation_id),
            sessions,
        )


async def test_someone_elses_conversation_is_404(use) -> None:
    first = _events((await _post({"message": "curry"})).text)[-1][1]
    resp = await _post(
        {"message": "hi", "conversation_id": first["conversation_id"]}, _as("u-intruder")
    )
    assert resp.status_code == 404
    assert (await _post({"message": "hi", "conversation_id": "nope"})).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"message": ""},
        {"message": "   "},
        {"message": "x" * (settings.assistant_max_message_chars + 1)},
        {"message": "hi", "user_id": "someone-else"},  # the caller comes from the token
        {"message": "hi", "lat": 91, "lng": 73},
    ],
)
async def test_bad_bodies_are_422(use, body) -> None:
    assert (await _post(body)).status_code == 422


async def test_only_customers(use) -> None:
    assert (await _post({"message": "hi"}, headers={})).status_code == 401
    assert (await _post({"message": "hi"}, _as("r-1", Role.RESTAURANT_ADMIN))).status_code == 403


# --- Heartbeats, disconnects, failures ---


class SlowRetriever(FakeRetriever):
    """Search that takes `seconds`; records whether it was cancelled."""

    def __init__(self, seconds: float) -> None:
        super().__init__()
        self.seconds, self.cancelled = seconds, False

    async def retrieve(self, query, filters):
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().retrieve(query, filters)


async def test_a_quiet_turn_sends_heartbeats(sessions) -> None:
    d = deps(*_models(), retriever=SlowRetriever(0.3))
    turn = await start_turn(ChatRequest(user_id=CUSTOMER, message="curry"), sessions)
    out = [chunk async for chunk in chat_api.events(turn, d, sessions, heartbeat_seconds=0.05)]
    assert ": heartbeat\n\n" in out
    assert out[-1].startswith("event: done")


async def test_a_client_that_goes_away_cancels_the_turn_and_it_is_saved(sessions) -> None:
    retriever = SlowRetriever(30)
    d = deps(*_models(), retriever=retriever)
    turn = await start_turn(ChatRequest(user_id=CUSTOMER, message="curry"), sessions)
    stream = chat_api.events(turn, d, sessions, heartbeat_seconds=5)
    assert (await anext(stream)).startswith("event: status")  # understanding
    await anext(stream)  # searching — now stuck in the slow search
    await stream.aclose()  # the client disconnects

    assert retriever.cancelled  # the work stopped, not just the output
    async with sessions() as s:
        row = (await s.scalars(select(Interaction))).one()
        conversation = await s.get(Conversation, turn.conversation_id)
    assert row.status == "interrupted" and row.error_code == "client_gone"
    assert conversation.busy_until is None and conversation.turn_count == 2


class _Broken(Live):
    async def quotes(self, by_restaurant):
        raise RuntimeError("bug in a dependency")


async def test_a_failure_mid_answer_ends_with_an_error_event(use, sessions) -> None:
    use(deps(*_models(), live=_Broken()))
    resp = await _post({"message": "curry"})
    assert resp.status_code == 200  # the stream had already started
    events = _events(resp.text)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "internal"
    assert "bug in a dependency" not in resp.text  # no internals leak
    async with sessions() as s:
        row = (await s.scalars(select(Interaction))).one()
        conversation = (await s.scalars(select(Conversation))).one()
    assert row.status == "error" and conversation.busy_until is None


# --- GET /v1/ai/orders/{id}/explain (step 4) ---


def _late_order() -> Any:
    from app.order_client import OrderRecord

    due = (datetime.now(UTC) - timedelta(minutes=12)).isoformat()
    order = {
        "id": "o-9",
        "order_number": 77,
        "restaurant_id": "r-1",
        "status": "confirmed",
        "total_amount": "900.00",
        "refunded_amount": "0",
        "created_at": due,
    }
    return OrderRecord(order, {"ready_at": due}, None)


async def _explain(order_id: str, headers: dict[str, str] | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(
            f"/v1/ai/orders/{order_id}/explain", headers=_as() if headers is None else headers
        )


async def test_an_order_explanation_streams_facts_then_words(use, sessions) -> None:
    from tests.assistant_fakes import Orders

    orders = Orders(_late_order())
    fast = chat(text="Sorry, order #77 is 12 minutes late in the kitchen.")
    use(deps(chat(), fast, orders=orders))
    resp = await _explain("o-9")
    assert resp.status_code == 200
    events = _events(resp.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "status" and kinds[-1] == "done"
    assert kinds.index("facts") < kinds.index("token")
    assert events[-1][1]["situation"] == "kitchen_late"
    text = "".join(d["text"] for k, d in events if k == "token")
    assert text == "Sorry, order #77 is 12 minutes late in the kitchen."
    async with sessions() as s:
        row = (await s.scalars(select(Interaction))).one()
    assert row.kind == "order_explain" and row.status == "answered"
    from app.models import OutboxEvent

    async with sessions() as s:
        event = (await s.scalars(select(OutboxEvent))).one()
    assert event.payload["kind"] == "order_explain" and event.payload["interaction_id"] == row.id
    assert row.prompt_version == "assistant_rules@v1,order_explain@v1"
    assert orders.tokens and all(t for t in orders.tokens)  # read with the caller's token


async def test_explaining_an_order_you_cant_see_is_404(use) -> None:
    from tests.assistant_fakes import Orders

    use(deps(chat(), chat(), orders=Orders()))
    assert (await _explain("o-someone-elses")).status_code == 404


async def test_order_explanations_are_for_customers(use) -> None:
    assert (await _explain("o-9", headers={})).status_code == 401
    assert (await _explain("o-9", _as("r-1", Role.RESTAURANT_ADMIN))).status_code == 403
