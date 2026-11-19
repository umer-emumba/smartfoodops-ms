"""Why an order is where it is — decided by rules in code, never by the
model (docs/week5-plan.md step 4, B5-D6).

`diagnose()` turns the order, delivery and payment records (and the
kitchen's load) into one situation code, the facts behind it as short
sentences with real numbers, and a plain explanation written by code. The
model only rewords those facts (app/assistant/order_explain.py); when it
can't, or adds a number the facts don't have, the code's explanation is
used as is.

Times are shown in Pakistan time (UTC+5, no daylight saving); minutes are
whole and at least 1. Free text someone typed (a restaurant's rejection
note, a cancellation or refund note) is kept apart in `notes`: it is shown
to the model fenced, as data."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.assistant.check import rs
from app.config import settings
from app.retrieval import Load

_PKT = timezone(timedelta(hours=settings.display_utc_offset_hours))

# The saga's `status_reason` codes (order-service FAILURE_REASONS and
# order_saga.py) → what happened, in words.
REASONS: dict[str, str] = {
    "out_of_stock": "an item in the order had run out",
    "restaurant_at_capacity": "the restaurant had too many orders to take another",
    "restaurant_unavailable": "the restaurant wasn't taking orders",
    "promotion_invalid": "the promo code couldn't be applied",
    "payment_declined": "the payment was declined",
    "restaurant_timeout": "the restaurant didn't accept the order in time",
    "restaurant_rejected": "the restaurant declined the order",
    "kitchen_timeout": "the restaurant didn't get the food ready in time",
    "no_rider_available": "no rider was available to deliver it",
    "delivery_cancelled": "the delivery was cancelled",
    "cancelled_by_customer": "you (the customer) cancelled it",
    "cancelled_by_restaurant_admin": "the restaurant cancelled it",
    "cancelled_by_system_admin": "our support team cancelled it",
    "cancelled_by_system": "it was cancelled automatically",
}


@dataclass
class Diagnosis:
    code: str  # e.g. kitchen_late, on_the_way, rejected, refunded
    order_number: int
    status: str
    facts: list[str]  # true sentences with the numbers the model may use
    text: str  # the explanation code writes: the fallback, and the number source
    notes: list[str] = field(default_factory=list)  # free text people typed (untrusted)


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except InvalidOperation:
        return Decimal(0)


def _minutes(a: datetime, b: datetime) -> int:
    return max(1, round(abs((b - a).total_seconds()) / 60))


def _clock(t: datetime) -> str:
    return t.astimezone(_PKT).strftime("%H:%M")


def _day(t: datetime) -> str:
    return t.astimezone(_PKT).strftime("%d %b")


def _who_cancelled(order: dict[str, Any]) -> str | None:
    for h in reversed(order.get("status_history") or []):
        if h.get("to_status") in ("cancelled", "rejected"):
            return h.get("actor_role")
    return None


def _reason(order: dict[str, Any]) -> tuple[str, str | None]:
    """The reason code, and any free text after it ("restaurant_rejected:
    we're closing early" → code + note)."""
    raw = str(order.get("status_reason") or "")
    code, _, note = raw.partition(":")
    code = code.strip()
    if code in REASONS:
        return code, note.strip() or None
    # Not a code: a note someone typed when cancelling.
    role = _who_cancelled(order) or "system"
    return f"cancelled_by_{role}", raw.strip() or None


def _kitchen(load: Load | None) -> list[str]:
    """Only a busy kitchen is a fact worth giving: a quiet one explains
    nothing, and the model read "1 of 20 orders" as an excuse."""
    if load is None or load.max_orders <= 0 or load.active_orders < 0.8 * load.max_orders:
        return []
    return [
        f"the kitchen is very busy: {load.active_orders} of {load.max_orders} orders in progress"
    ]


def diagnose(
    order: dict[str, Any],
    delivery: dict[str, Any] | None,
    payment: dict[str, Any] | None,
    load: Load | None,
    now: datetime | None = None,
) -> Diagnosis:
    now = now or datetime.now(UTC)
    status = str(order.get("status"))
    number = int(order.get("order_number") or 0)
    d = delivery or {}
    facts: list[str] = []
    notes: list[str] = []

    def done(code: str, text: str) -> Diagnosis:
        return Diagnosis(code, number, status, facts, text, notes)

    refunded = _money(order.get("refunded_amount"))
    if refunded > 0:
        total = _money(order.get("total_amount"))
        when = _time(order.get("refunded_at"))
        part = "full" if refunded >= total else "partial"
        facts.append(f"a {part} refund of {rs(float(refunded))} of {rs(float(total))}")
        if when:
            facts.append(f"refunded on {_day(when)}")
        for h in order.get("status_history") or []:
            reason = str(h.get("reason") or "")
            if reason.startswith("refunded ") and ":" in reason:
                notes.append(reason.split(":", 1)[1].strip())
        facts.append("refunds reach the original payment method")
        on = f" on {_day(when)}" if when else ""
        return done(
            "refunded",
            f"Order #{number}: we refunded {rs(float(refunded))} of {rs(float(total))}{on}. "
            "It goes back to the payment method you used.",
        )

    if status in ("cancelled", "rejected"):
        code, note = _reason(order)
        if note:
            notes.append(note)
        if code == "payment_declined" and payment and payment.get("failure_reason"):
            notes.append(str(payment["failure_reason"]))
        facts.append(REASONS[code])
        charged = payment and payment.get("status") in ("captured",)
        facts.append(
            "you were charged and will be refunded" if charged else "you weren't charged for it"
        )
        verb = "was declined" if status == "rejected" else "was cancelled"
        return done(
            "rejected" if status == "rejected" else "cancelled",
            f"Order #{number} {verb}: {REASONS[code]}. "
            + ("You'll be refunded." if charged else "You weren't charged."),
        )

    if status == "delivered":
        delivered = _time(order.get("delivered_at"))
        eta = _time(d.get("dropoff_eta"))
        if delivered:
            facts.append(f"delivered at {_clock(delivered)} on {_day(delivered)}")
        if delivered and eta and delivered > eta + timedelta(minutes=1):
            late = _minutes(eta, delivered)
            facts.append(f"{late} minutes after the estimate of {_clock(eta)}")
            return done(
                "delivered_late",
                f"Order #{number} was delivered at {_clock(delivered)}, "
                f"{late} minutes after the estimate.",
            )
        at = f" at {_clock(delivered)}" if delivered else ""
        return done("delivered", f"Order #{number} was delivered{at}.")

    if status in ("created", "awaiting_restaurant_acceptance"):
        created = _time(order.get("created_at")) or now
        waited = _minutes(created, now)
        facts.append(f"sent to the restaurant {waited} minutes ago")
        facts.append("the restaurant hasn't accepted it yet")
        return done(
            "awaiting_restaurant",
            f"Order #{number} is waiting for the restaurant to accept it "
            f"(sent {waited} minutes ago).",
        )

    ready_at = _time(d.get("ready_at"))
    food_ready = _time(d.get("food_ready_at"))

    # The kitchen is late whether or not a rider is already waiting for it.
    if (
        status in ("confirmed", "assigned")
        and food_ready is None
        and ready_at
        and (now > ready_at + timedelta(minutes=1))
    ):
        late = _minutes(ready_at, now)
        facts.append(f"the food was due at {_clock(ready_at)}, {late} minutes ago")
        facts.append("the restaurant hasn't marked it ready yet")
        if status == "assigned":
            facts.append("a rider is assigned and will collect it once it's ready")
        facts.extend(_kitchen(load))
        return done(
            "kitchen_late",
            f"Order #{number} is taking longer in the kitchen: the food was due at "
            f"{_clock(ready_at)} and is {late} minutes late.",
        )

    if status == "confirmed":
        facts.extend(_kitchen(load))
        if food_ready is None:
            due = f" around {_clock(ready_at)}" if ready_at else ""
            facts.insert(0, f"the restaurant is preparing it; ready{due}")
            return done("preparing", f"Order #{number} is being prepared; ready{due}.")
        waiting = _minutes(food_ready, now)
        tries = int(d.get("reassignment_count") or 0)
        facts.append(f"the food has been ready for {waiting} minutes")
        facts.append("we're finding a rider")
        if tries:
            facts.append(f"{tries} rider(s) couldn't take it, so we're asking others")
        return done(
            "finding_rider",
            f"Order #{number} is ready and we're finding a rider "
            f"(food ready {waiting} minutes ago).",
        )

    if status == "assigned":
        pickup = _time(d.get("pickup_eta"))
        facts.append("a rider is on the way to the restaurant")
        if pickup and now > pickup + timedelta(minutes=1):
            late = _minutes(pickup, now)
            facts.append(f"the rider was expected there at {_clock(pickup)}, {late} minutes ago")
            return done(
                "rider_late",
                f"Order #{number}: the rider is running {late} minutes late to the restaurant.",
            )
        at = f" around {_clock(pickup)}" if pickup else ""
        if pickup:
            facts.append(f"the rider should be there{at}")
        return done("rider_assigned", f"Order #{number}: a rider will pick it up{at}.")

    if status == "picked_up":
        eta = _time(d.get("dropoff_eta"))
        facts.append("the rider has the food and is on the way to you")
        if eta and now > eta + timedelta(minutes=1):
            late = _minutes(eta, now)
            facts.append(f"it was expected at {_clock(eta)}, {late} minutes ago")
            return done(
                "running_late",
                f"Order #{number} is on the way but running {late} minutes late "
                f"(expected at {_clock(eta)}).",
            )
        at = f" around {_clock(eta)}" if eta else ""
        if eta:
            facts.append(f"expected to arrive{at}")
        return done("on_the_way", f"Order #{number} is on the way; arriving{at}.")

    facts.append(f"its status is {status}")
    return done("unknown", f"Order #{number} is {status.replace('_', ' ')}.")
