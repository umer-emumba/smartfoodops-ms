from decimal import Decimal

from app.models import MenuItem, Restaurant
from app.schemas import QuoteResponse


def test_quote_serializes_orm_rows_with_exact_prices() -> None:
    restaurant = Restaurant(id="r1", status="suspended", is_accepting_orders=True)
    item = MenuItem(
        id="m1", name="Wrap", price=Decimal("0.10"), is_available=True, stock_count=None
    )
    body = QuoteResponse.model_validate({"restaurant": restaurant, "items": [item]})
    data = body.model_dump(mode="json")
    # Status is passed through as-is — order-service applies the visibility rule.
    assert data["restaurant"] == {"id": "r1", "status": "suspended", "is_accepting_orders": True}
    # Decimal travels as a string, so order-service rebuilds the exact value.
    assert data["items"][0]["price"] == "0.10"
    assert Decimal(data["items"][0]["price"]) == Decimal("0.10")
