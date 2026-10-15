"""Why an order was rejected or cancelled (`orders.status_reason`), as a
bounded Prometheus label — used by order-service's saga metrics and by
analytics-service. Free text (a customer's own cancellation reason) can't
become a label value, or every distinct text would be a new series."""

KNOWN_REASONS = frozenset(
    {
        "out_of_stock",
        "restaurant_at_capacity",
        "restaurant_unavailable",
        "promotion_invalid",
        "payment_declined",
        "restaurant_timeout",
        "restaurant_rejected",
        "no_rider_available",
        "system_error",
    }
)


def reason_code(reason: str | None) -> str | None:
    """`restaurant_rejected: kitchen closed` → `restaurant_rejected`."""
    return reason.split(":", 1)[0].strip() if reason else None


def reason_label(reason: str | None) -> str:
    code = reason_code(reason)
    if code is None:
        return ""
    if code in KNOWN_REASONS or code.startswith("cancelled_by_"):
        return code
    return "other"  # free text from whoever cancelled
