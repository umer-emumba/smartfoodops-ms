from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from sfo_events.payloads import EventPayload


class Topic(StrEnum):
    """One topic per aggregate, not per event type — see
    docs/architecture.md#conventions."""

    ORDER = "order-events"
    DELIVERY = "delivery-events"
    PAYMENT = "payment-events"


def dlq_topic(topic: str) -> str:
    """Where a consumer parks a message it can't process (week3-plan W3-D5)."""
    return f"{topic}.dlq"


class EventType(StrEnum):
    ORDER_PLACED = "OrderPlaced"
    ORDER_AWAITING_RESTAURANT = "OrderAwaitingRestaurant"
    ORDER_CONFIRMED = "OrderConfirmed"
    ORDER_REJECTED = "OrderRejected"
    ORDER_CANCELLED = "OrderCancelled"
    PAYMENT_SUCCESSFUL = "PaymentSuccessful"
    REFUND_PROCESSED = "RefundProcessed"
    RIDER_ASSIGNED = "RiderAssigned"
    ORDER_PICKED_UP = "OrderPickedUp"
    ORDER_DELIVERED = "OrderDelivered"
    DELIVERY_STATUS_UPDATED = "DeliveryStatusUpdated"
    DELIVERY_ETA_UPDATED = "DeliveryEtaUpdated"
    DELIVERY_FOOD_READY = "DeliveryFoodReady"
    RIDER_SHIFT_CHANGED = "RiderShiftChanged"


# The topic each event is published to. RiderShiftChanged rides on
# delivery-events: delivery-service owns riders, and keying by rider_id keeps
# one rider's shift changes in order.
EVENT_TOPICS: dict[EventType, Topic] = {
    EventType.ORDER_PLACED: Topic.ORDER,
    EventType.ORDER_AWAITING_RESTAURANT: Topic.ORDER,
    EventType.ORDER_CONFIRMED: Topic.ORDER,
    EventType.ORDER_REJECTED: Topic.ORDER,
    EventType.ORDER_CANCELLED: Topic.ORDER,
    EventType.RIDER_ASSIGNED: Topic.ORDER,
    EventType.ORDER_PICKED_UP: Topic.ORDER,
    EventType.ORDER_DELIVERED: Topic.ORDER,
    EventType.PAYMENT_SUCCESSFUL: Topic.PAYMENT,
    EventType.REFUND_PROCESSED: Topic.PAYMENT,
    EventType.DELIVERY_STATUS_UPDATED: Topic.DELIVERY,
    EventType.DELIVERY_ETA_UPDATED: Topic.DELIVERY,
    EventType.DELIVERY_FOOD_READY: Topic.DELIVERY,
    EventType.RIDER_SHIFT_CHANGED: Topic.DELIVERY,
}


class DomainEvent(BaseModel):
    """Envelope every outbox row/Kafka message carries. `aggregate_id` is the
    Kafka partition key (order_id/delivery_id/payment_id/rider_id) so
    per-entity ordering is preserved. `occurred_at` is the outbox row's
    `created_at` — payloads don't repeat it."""

    event_id: str
    event_type: EventType
    aggregate_id: str
    occurred_at: datetime
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def typed_payload(self) -> "EventPayload":
        """The payload as its event type's model (sfo_events.payloads).
        Lenient: fields a newer producer added are ignored."""
        from sfo_events.payloads import parse_payload

        return parse_payload(self.event_type, self.payload)
