from datetime import datetime

from pydantic import BaseModel


class PaymentResponse(BaseModel):
    id: str
    order_id: str
    customer_id: str
    amount: float
    status: str
    payment_method: str
    failure_reason: str | None
    refunded_amount: float
    created_at: datetime
    authorized_at: datetime | None
    refunded_at: datetime | None

    model_config = {"from_attributes": True}
