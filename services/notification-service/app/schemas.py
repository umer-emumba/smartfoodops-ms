from datetime import datetime

from pydantic import BaseModel


class NotificationResponse(BaseModel):
    id: str
    related_order_id: str | None
    type: str
    channel: str
    content: str
    status: str
    created_at: datetime
    sent_at: datetime | None
    read_at: datetime | None

    model_config = {"from_attributes": True}
