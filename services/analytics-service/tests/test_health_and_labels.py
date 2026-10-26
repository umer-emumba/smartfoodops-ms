import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient
from sfo_events.reasons import reason_code, reason_label


async def test_health_reports_status() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code in (200, 503)
    assert "status" in response.json()


@pytest.mark.parametrize(
    ("reason", "code", "label"),
    [
        (None, None, ""),
        ("payment_declined", "payment_declined", "payment_declined"),
        ("restaurant_rejected: kitchen closed", "restaurant_rejected", "restaurant_rejected"),
        ("cancelled_by_customer", "cancelled_by_customer", "cancelled_by_customer"),
        ("changed my mind", "changed my mind", "other"),
    ],
)
def test_reasons_become_bounded_labels(reason: str | None, code: str | None, label: str) -> None:
    assert (reason_code(reason), reason_label(reason)) == (code, label)
