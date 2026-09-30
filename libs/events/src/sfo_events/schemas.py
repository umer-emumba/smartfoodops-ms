from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Topic(StrEnum):
    """One topic per aggregate, not per event type — see
    docs/architecture.md#conventions."""

    ORDER = "order-events"
    DELIVERY = "delivery-events"
    PAYMENT = "payment-events"


class EventType(StrEnum):
    ORDER_PLACED = "OrderPlaced"
    ORDER_CONFIRMED = "OrderConfirmed"
    ORDER_REJECTED = "OrderRejected"
    ORDER_CANCELLED = "OrderCancelled"
    PAYMENT_SUCCESSFUL = "PaymentSuccessful"
    REFUND_PROCESSED = "RefundProcessed"
    RIDER_ASSIGNED = "RiderAssigned"
    ORDER_PICKED_UP = "OrderPickedUp"
    ORDER_DELIVERED = "OrderDelivered"
    DELIVERY_STATUS_UPDATED = "DeliveryStatusUpdated"


class DomainEvent(BaseModel):
    """Envelope every outbox row/Kafka message carries. `aggregate_id` is the
    Kafka partition key (order_id/delivery_id/payment_id) so per-entity
    ordering is preserved."""

    event_id: str
    event_type: EventType
    aggregate_id: str
    occurred_at: datetime
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
