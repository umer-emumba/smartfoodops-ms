"""Content drafts against a real ai_db (step 5): the worker's attempt
(ready / retry later / failed), and the owner-only API — request (202 and
queued), read, accept (applied through restaurant-service, once), reject."""

from typing import Any

import pytest
from app.api import chat as chat_api
from app.api import content as content_api
from app.config import settings
from app.content import data, service
from app.content.kinds import DescriptionOut
from app.llm import LLMRateLimited
from app.main import app
from app.models import ContentDraft, Interaction
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sfo_auth import Role
from sfo_auth.testing import make_test_token
from sqlalchemy import select

from tests.assistant_fakes import chat, llm
from tests.test_content import _facts

pytestmark = pytest.mark.db

OWNER = "u-owner"


def _as(user_id: str = OWNER, role: Role = Role.RESTAURANT_ADMIN) -> dict[str, str]:
    token = make_test_token(user_id=user_id, role=role, jwks_url=settings.jwt_jwks_url)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(sessions, monkeypatch):
    """r-1 belongs to OWNER; the index holds `_facts()`; restaurant-service
    calls made by accept are recorded, not sent."""
    queued: list[str] = []
    applied: list[tuple[str, str, dict, str]] = []

    async def owner_of(restaurant_id: str) -> str:
        if restaurant_id != "r-1":
            from sfo_common.errors import NotFoundError

            raise NotFoundError("Restaurant not found")
        return OWNER

    async def facts(restaurant_id: str, **_: Any):
        return _facts()

    async def apply(method: str, path: str, body: dict, token: str) -> dict:
        applied.append((method, path, body, token))
        return {"id": "promo-1"}

    monkeypatch.setattr(service, "owner_of", owner_of)
    monkeypatch.setattr(data, "facts", facts)
    monkeypatch.setattr(service, "_apply", apply)
    app.dependency_overrides[chat_api.get_sessions] = lambda: sessions
    app.dependency_overrides[content_api.get_enqueue] = lambda: queued.append
    yield {"queued": queued, "applied": applied}
    app.dependency_overrides.clear()


async def _call(method: str, path: str, headers: dict | None = None, **kw: Any):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, path, headers=_as() if headers is None else headers, **kw)


async def _requested(kind: str = "menu_description", **params: Any) -> dict:
    body = {"restaurant_id": "r-1", "kind": kind, **params}
    if kind == "menu_description":
        body.setdefault("menu_item_id", "m-1")
    resp = await _call("POST", "/v1/ai/content", json=body)
    assert resp.status_code == 202, resp.text
    return resp.json()


# --- Requesting: owner only, queued, nothing generated in the request ---


async def test_the_owner_gets_202_and_the_draft_is_queued(world) -> None:
    draft = await _requested()
    assert draft["status"] == "pending" and draft["params"] == {"menu_item_id": "m-1"}
    assert world["queued"] == [draft["id"]]


async def test_only_the_restaurants_owner(world) -> None:
    body = {"restaurant_id": "r-1", "kind": "feedback_summary"}
    assert (await _call("POST", "/v1/ai/content", _as("u-other"), json=body)).status_code == 403
    customer = _as("u-c", Role.CUSTOMER)
    assert (await _call("POST", "/v1/ai/content", customer, json=body)).status_code == 403
    missing = {"restaurant_id": "r-404", "kind": "feedback_summary"}
    assert (await _call("POST", "/v1/ai/content", json=missing)).status_code == 404
    no_item = {"restaurant_id": "r-1", "kind": "menu_description"}
    assert (await _call("POST", "/v1/ai/content", json=no_item)).status_code == 422
    assert world["queued"] == []


# --- The worker's attempt ---


async def test_a_draft_is_generated_and_checked(world, sessions) -> None:
    draft = await _requested()
    fast = chat(DescriptionOut(description="Chicken in a fiery karahi gravy."))
    assert await service.generate(sessions, llm(chat(), fast), draft["id"]) == "ready"
    got = (await _call("GET", f"/v1/ai/content/{draft['id']}")).json()
    assert got["status"] == "ready"
    assert got["output"]["description"] == "Chicken in a fiery karahi gravy."
    async with sessions() as s:
        row = (await s.scalars(select(Interaction))).one()
    assert row.kind == "content" and row.intent == "menu_description"
    assert row.prompt_version == "content_rules@v1,content_description@v1"
    # A second task for the same draft (sweep, redelivery) does nothing.
    assert await service.generate(sessions, llm(chat(), fast), draft["id"]) == "skipped"


async def test_a_rate_limit_leaves_it_pending_for_another_attempt(world, sessions) -> None:
    draft = await _requested()
    busy = chat(("DescriptionOut", LLMRateLimited("429")))
    assert await service.generate(sessions, llm(chat(), busy), draft["id"]) == "pending"
    async with sessions() as s:
        row = await s.get(ContentDraft, draft["id"])
    assert row.attempts == 1 and row.error == "llm_rate_limited"


async def test_no_llm_or_a_missing_dish_fails_with_the_reason(world, sessions) -> None:
    draft = await _requested()
    unconfigured = llm(chat(), chat(), llm_api_key=SecretStr(""))
    unconfigured._factory = None
    assert await service.generate(sessions, unconfigured, draft["id"]) == "failed"
    gone = await _requested(menu_item_id="m-404")
    fast = chat(DescriptionOut(description="x" * 20))
    assert await service.generate(sessions, llm(chat(), fast), gone["id"]) == "failed"
    got = (await _call("GET", f"/v1/ai/content/{gone['id']}")).json()
    assert "isn't on this restaurant's menu" in got["error"]


# --- Deciding ---


async def _ready(sessions, kind: str = "menu_description", **params: Any) -> str:
    draft = await _requested(kind, **params)
    answers = {
        "menu_description": DescriptionOut(description="Chicken in a fiery karahi gravy."),
    }
    if kind == "promotion":
        from tests.test_content import _promo

        smart, fast = chat(_promo()), chat()
    else:
        smart, fast = chat(), chat(answers[kind])
    assert await service.generate(sessions, llm(smart, fast), draft["id"]) == "ready"
    return str(draft["id"])


async def test_accepting_a_description_patches_the_menu_item_as_the_owner(world, sessions):
    draft_id = await _ready(sessions)
    resp = await _call("POST", f"/v1/ai/content/{draft_id}/accept")
    assert resp.status_code == 200 and resp.json()["status"] == "accepted"
    [(method, path, body, token)] = world["applied"]
    assert (method, path) == ("PATCH", "/v1/restaurants/r-1/menu/items/m-1")
    assert body == {"description": "Chicken in a fiery karahi gravy."} and token
    # Once only: a second click is 409, nothing applied twice.
    assert (await _call("POST", f"/v1/ai/content/{draft_id}/accept")).status_code == 409
    assert len(world["applied"]) == 1


async def test_accepting_a_promotion_creates_it(world, sessions) -> None:
    draft_id = await _ready(sessions, "promotion")
    resp = await _call("POST", f"/v1/ai/content/{draft_id}/accept")
    assert resp.json()["applied"] == {"promotion_id": "promo-1"}
    [(method, path, body, _)] = world["applied"]
    assert (method, path) == ("POST", "/v1/restaurants/r-1/promotions")
    assert body["code"] == "LUNCH15" and body["discount_value"] == "15.0"
    assert set(body) == {
        "code",
        "discount_type",
        "discount_value",
        "valid_from",
        "valid_until",
        "max_uses",
    }


async def test_rejecting_changes_nothing(world, sessions) -> None:
    draft_id = await _ready(sessions)
    resp = await _call("POST", f"/v1/ai/content/{draft_id}/reject")
    assert resp.json()["status"] == "rejected" and world["applied"] == []
    assert (await _call("POST", f"/v1/ai/content/{draft_id}/accept")).status_code == 409


async def test_a_pending_draft_cant_be_accepted_and_others_cant_see_it(world) -> None:
    draft = await _requested()
    assert (await _call("POST", f"/v1/ai/content/{draft['id']}/accept")).status_code == 409
    other = await _call("GET", f"/v1/ai/content/{draft['id']}", _as("u-other"))
    assert other.status_code == 404
    listed = (await _call("GET", "/v1/ai/content", params={"restaurant_id": "r-1"})).json()
    assert [d["id"] for d in listed["items"]] == [draft["id"]]


async def test_generation_and_decision_are_analytics_events(world, sessions) -> None:
    from app.models import OutboxEvent

    draft_id = await _ready(sessions)
    await _call("POST", f"/v1/ai/content/{draft_id}/reject")
    async with sessions() as s:
        rows = (await s.scalars(select(OutboxEvent).order_by(OutboxEvent.created_at))).all()
    assert [r.event_type for r in rows] == ["AiInteractionRecorded", "ContentDraftDecided"]
    assert rows[0].payload["kind"] == "content" and rows[0].payload["restaurant_id"] == "r-1"
    assert rows[1].payload == {
        "draft_id": draft_id,
        "restaurant_id": "r-1",
        "owner_id": OWNER,
        "kind": "menu_description",
        "decision": "rejected",
    }
