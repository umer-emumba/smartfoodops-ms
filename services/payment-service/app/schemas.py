from datetime import datetime

from pydantic import BaseModel
from sfo_common.money import MoneyAmount


class PaymentResponse(BaseModel):
    id: str
    order_id: str
    customer_id: str
    amount: MoneyAmount
    status: str
    payment_method: str
    failure_reason: str | None
    refunded_amount: MoneyAmount
    created_at: datetime
    authorized_at: datetime | None
    captured_at: datetime | None
    voided_at: datetime | None
    refunded_at: datetime | None

    model_config = {"from_attributes": True}
