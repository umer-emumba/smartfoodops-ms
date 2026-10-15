from typing import Any

import pytest
from pydantic import ValidationError
from sfo_events import (
    PAYLOAD_MODELS,
    DomainEvent,
    EventType,
    new_outbox_row,
    parse_payload,
    validate_payload,
)
from sfo_events.consumer import parse_event
from sfo_events.payloads import envelope_model

from tests.samples import SAMPLE_PAYLOADS, sample_event


def test_every_event_type_has_a_model_topic_and_sample() -> None:
    from sfo_events import EVENT_TOPICS

    assert set(PAYLOAD_MODELS) == set(EventType)
    assert set(EVENT_TOPICS) == set(EventType)
    assert set(SAMPLE_PAYLOADS) == set(EventType)


@pytest.mark.parametrize("event_type", list(EventType))
def test_round_trip_through_the_outbox_form(event_type: EventType) -> None:
    stored = validate_payload(event_type, SAMPLE_PAYLOADS[event_type])
    assert parse_payload(event_type, stored) == PAYLOAD_MODELS[event_type].model_validate(
        SAMPLE_PAYLOADS[event_type]
    )


@pytest.mark.parametrize("event_type", list(EventType))
def test_round_trip_through_the_kafka_envelope(event_type: EventType) -> None:
    event = sample_event(event_type)
    wire = event.model_dump(mode="json")
    # What the producer validates against the registered schema...
    envelope_model(event_type).model_validate(wire)
    # ...and what a consumer gets back.
    parsed = parse_event(wire)
    assert parsed == event
    assert parsed is not None and parsed.typed_payload() == event.typed_payload()


def test_amounts_are_stored_as_strings() -> None:
    stored = validate_payload(EventType.ORDER_PLACED, SAMPLE_PAYLOADS[EventType.ORDER_PLACED])
    assert stored["total_amount"] == "25.50"


def test_producer_rejects_unknown_fields() -> None:
    payload = {**SAMPLE_PAYLOADS[EventType.ORDER_CONFIRMED], "rider_name": "typo"}
    with pytest.raises(ValidationError):
        validate_payload(EventType.ORDER_CONFIRMED, payload)


def test_producer_rejects_missing_fields() -> None:
    payload = dict(SAMPLE_PAYLOADS[EventType.ORDER_CONFIRMED])
    del payload["restaurant_admin_id"]
    with pytest.raises(ValidationError):
        validate_payload(EventType.ORDER_CONFIRMED, payload)


def test_consumer_ignores_fields_a_newer_producer_added() -> None:
    stored = validate_payload(EventType.ORDER_CONFIRMED, SAMPLE_PAYLOADS[EventType.ORDER_CONFIRMED])
    parsed = parse_payload(EventType.ORDER_CONFIRMED, {**stored, "added_later": 1})
    assert parsed.model_dump(mode="json") == stored


def test_envelope_pins_the_event_type() -> None:
    wire = sample_event(EventType.ORDER_CONFIRMED).model_dump(mode="json")
    with pytest.raises(ValidationError):
        envelope_model(EventType.ORDER_DELIVERED).model_validate(wire)


def test_unknown_event_type_is_skipped_not_rejected() -> None:
    wire = sample_event(EventType.ORDER_CONFIRMED).model_dump(mode="json")
    assert parse_event({**wire, "event_type": "SomethingNew"}) is None


def test_malformed_known_event_is_rejected() -> None:
    wire = sample_event(EventType.ORDER_CONFIRMED).model_dump(mode="json")
    del wire["payload"]["order_id"]
    with pytest.raises(ValidationError):
        parse_event(wire)


class _Row:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def test_new_outbox_row_validates_and_normalizes() -> None:
    row = new_outbox_row(
        _Row,
        aggregate_id="ord-1",
        event_type=EventType.ORDER_PLACED,
        payload=SAMPLE_PAYLOADS[EventType.ORDER_PLACED],
    )
    assert row.payload["total_amount"] == "25.50"  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        new_outbox_row(_Row, aggregate_id="ord-1", event_type=EventType.ORDER_PLACED, payload={})


def test_domain_event_payload_defaults_to_empty() -> None:
    # The Week 2 envelope shape still validates (payload checked separately).
    assert (
        DomainEvent.model_validate(
            {
                "event_id": "e",
                "event_type": "OrderPlaced",
                "aggregate_id": "a",
                "occurred_at": "2026-10-15T12:00:00Z",
            }
        ).payload
        == {}
    )
