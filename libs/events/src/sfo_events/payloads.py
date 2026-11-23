"""Typed payload per event type (week3-plan W3-D3, W3-D8).

Producers are strict: `new_outbox_row` rejects a payload with a missing,
mistyped or unknown field, so a bad event fails the business transaction
instead of reaching Kafka. Consumers are lenient (`parse_payload`): fields a
newer producer added are dropped, so adding an optional field never breaks an
older consumer.

The Kafka value is the whole envelope; `envelope_json_schema` is what goes to
Schema Registry. Its `title` is the event type, which TopicRecordNameStrategy
turns into the subject `<topic>-<EventType>`. The models are closed
(`additionalProperties: false`), so under BACKWARD compatibility adding an
optional field is allowed and removing or retyping one is not.

Payloads carry everything consumers need — recipients and dimensions — so
no consumer ever calls another service (W3-D8).
"""

from datetime import datetime
from decimal import Decimal
from functools import cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model

from sfo_events.schemas import EventType


class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OrderItemRef(EventPayload):
    menu_item_id: str
    quantity: int


class OrderPlacedPayload(EventPayload):
    order_id: str
    order_number: int
    customer_id: str
    restaurant_id: str
    restaurant_admin_id: str | None
    total_amount: Decimal
    items: list[OrderItemRef]


class OrderLine(EventPayload):
    menu_item_id: str
    name: str
    quantity: int
    unit_price: Decimal


class OrderTransitionPayload(EventPayload):
    """Every order status event after OrderPlaced. `reason` is set on
    rejections and cancellations (e.g. `restaurant_rejected`). `items` only
    on OrderDelivered (week4-plan step 3): what was ordered, by name, for
    ai-service's order history — None on older events."""

    order_id: str
    order_number: int
    customer_id: str
    restaurant_id: str
    restaurant_admin_id: str | None
    total_amount: Decimal
    from_status: str
    to_status: str
    reason: str | None = None
    items: list[OrderLine] | None = None


class PaymentSuccessfulPayload(EventPayload):
    payment_id: str
    order_id: str
    customer_id: str
    amount: Decimal
    status: str


class RefundProcessedPayload(EventPayload):
    payment_id: str
    order_id: str
    customer_id: str
    refund_id: str
    amount: Decimal
    total_refunded: Decimal
    reason: str | None = None


class DeliveryStatusUpdatedPayload(EventPayload):
    """`rider_id`/`rider_user_id` are the rider holding the delivery after the
    change (None once released). `customer_id` is None only on a cancelled
    placeholder delivery. `from_status` is None when the delivery is created.

    `released_rider_user_id`: the rider who just lost it — their offer was
    declined or expired, or they dropped out — so their app can be told
    (rider_user_id is already None by then). `pickup_eta`/`dropoff_eta` are
    the delivery's current estimates (None until computed, and after a
    release)."""

    delivery_id: str
    order_id: str
    customer_id: str | None
    rider_id: str | None
    rider_user_id: str | None
    from_status: str | None
    to_status: str
    reason: str | None = None
    released_rider_user_id: str | None = None
    pickup_eta: datetime | None = None
    dropoff_eta: datetime | None = None


class DeliveryEtaUpdatedPayload(EventPayload):
    """New estimates for the assigned rider, without a status change."""

    delivery_id: str
    order_id: str
    customer_id: str | None
    rider_id: str
    status: str
    pickup_eta: datetime
    dropoff_eta: datetime


class DeliveryFoodReadyPayload(EventPayload):
    """The restaurant marked the food ready, without a status change. The
    ETAs are recomputed from it once a rider holds the delivery (None
    before)."""

    delivery_id: str
    order_id: str
    customer_id: str | None
    rider_id: str | None
    rider_user_id: str | None
    status: str
    food_ready_at: datetime
    pickup_eta: datetime | None = None
    dropoff_eta: datetime | None = None


class RiderShiftChangedPayload(EventPayload):
    """Only offline ↔ on shift (W3-D8c); the saga's available ↔ busy flips
    don't emit it. `reason`: `rider` (they chose), `suspended`, `dropped_out`."""

    rider_id: str
    rider_user_id: str
    from_status: str
    to_status: str
    on_shift: bool
    reason: str


# --- restaurant-events (week4-plan step 2) ---
# Full snapshots, not diffs: ai-service's indexer builds its documents from
# the event alone. `source_version` is the restaurant's catalog_version at the
# write — it rises with every event of that restaurant (items and reviews
# included), so a consumer can drop anything not newer than what it has.


class RestaurantUpsertedPayload(EventPayload):
    restaurant_id: str
    name: str
    description: str
    city: str
    address: str
    lat: float
    lng: float
    cuisine_type: str | None
    status: str
    is_accepting_orders: bool
    avg_prep_time_minutes: int
    avg_rating: float | None
    review_count: int
    source_version: int


class RestaurantStatusChangedPayload(EventPayload):
    """`reason` is the admin's note, if any."""

    restaurant_id: str
    from_status: str
    to_status: str
    reason: str | None = None
    source_version: int


class MenuItemUpsertedPayload(EventPayload):
    """Re-sent for every item of a restaurant when its name, cuisine or
    location changes, and for a category's items when it's renamed — they
    are part of the item's search text and filters. `in_stock`: unlimited
    stock or some left; re-sent when that flips, not on every sale."""

    menu_item_id: str
    restaurant_id: str
    restaurant_name: str
    restaurant_status: str
    cuisine_type: str | None
    city: str
    lat: float
    lng: float
    category_id: str
    category_name: str
    name: str
    description: str
    price: Decimal
    dietary_tags: list[str]
    spice_level: int
    calories: int | None
    is_available: bool
    in_stock: bool
    source_version: int


class MenuItemDeletedPayload(EventPayload):
    menu_item_id: str
    restaurant_id: str
    source_version: int


class ReviewSubmittedPayload(EventPayload):
    """A review and the restaurant's rating after it. No customer_id: the
    review is published, its author isn't."""

    order_id: str
    restaurant_id: str
    restaurant_name: str
    rating: int
    text: str
    item_ids: list[str]
    created_at: datetime
    avg_rating: float
    review_count: int
    source_version: int


PAYLOAD_MODELS: dict[EventType, type[EventPayload]] = {
    EventType.ORDER_PLACED: OrderPlacedPayload,
    EventType.ORDER_AWAITING_RESTAURANT: OrderTransitionPayload,
    EventType.ORDER_CONFIRMED: OrderTransitionPayload,
    EventType.ORDER_REJECTED: OrderTransitionPayload,
    EventType.ORDER_CANCELLED: OrderTransitionPayload,
    EventType.RIDER_ASSIGNED: OrderTransitionPayload,
    EventType.ORDER_PICKED_UP: OrderTransitionPayload,
    EventType.ORDER_DELIVERED: OrderTransitionPayload,
    EventType.PAYMENT_SUCCESSFUL: PaymentSuccessfulPayload,
    EventType.REFUND_PROCESSED: RefundProcessedPayload,
    EventType.DELIVERY_STATUS_UPDATED: DeliveryStatusUpdatedPayload,
    EventType.DELIVERY_ETA_UPDATED: DeliveryEtaUpdatedPayload,
    EventType.DELIVERY_FOOD_READY: DeliveryFoodReadyPayload,
    EventType.RIDER_SHIFT_CHANGED: RiderShiftChangedPayload,
    EventType.RESTAURANT_UPSERTED: RestaurantUpsertedPayload,
    EventType.RESTAURANT_STATUS_CHANGED: RestaurantStatusChangedPayload,
    EventType.MENU_ITEM_UPSERTED: MenuItemUpsertedPayload,
    EventType.MENU_ITEM_DELETED: MenuItemDeletedPayload,
    EventType.REVIEW_SUBMITTED: ReviewSubmittedPayload,
}


def validate_payload(event_type: EventType, payload: dict[str, Any]) -> dict[str, Any]:
    """Producer side: strict validation; returns the JSON-ready form stored
    in the outbox (Decimals become strings)."""
    model = PAYLOAD_MODELS[event_type].model_validate(payload)
    return model.model_dump(mode="json")


def parse_payload(event_type: EventType, payload: dict[str, Any]) -> EventPayload:
    """Consumer side: unknown fields are dropped before validation."""
    model_cls = PAYLOAD_MODELS[event_type]
    known = {k: v for k, v in payload.items() if k in model_cls.model_fields}
    return model_cls.model_validate(known)


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    aggregate_id: str
    occurred_at: datetime
    correlation_id: str | None = None


@cache
def envelope_model(event_type: EventType) -> type[BaseModel]:
    """The envelope with `event_type` pinned and `payload` typed. The class
    name becomes the JSON Schema title, i.e. the registry record name."""
    model: type[BaseModel] = create_model(
        event_type.value,
        __base__=_Envelope,
        event_type=(Literal[event_type.value], ...),  # type: ignore[valid-type]
        payload=(PAYLOAD_MODELS[event_type], ...),
    )
    return model


def envelope_json_schema(event_type: EventType) -> dict[str, Any]:
    """What's registered for `event_type` — the shape of the serialized
    message, so Decimals are strings."""
    return envelope_model(event_type).model_json_schema(mode="serialization")
